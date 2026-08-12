# ClickScope

A read-only [ClickHouse](https://clickhouse.com) MCP server that lets an agent explore your
schema and **cost a query before running it**.

Most database MCP servers give a model one blunt instrument: "run this SQL". On a table with
billions of rows that is how you get a surprise full scan. ClickScope exposes ClickHouse's own
planner instead — `EXPLAIN ESTIMATE` tells you how many parts, rows and marks a query *would*
read, before a single byte is touched.

Works with any ClickHouse: self-hosted, clustered, local, or Cloud.

## Install

### As a Claude Code plugin (recommended)

```
/plugin marketplace add sgr-xd/clickscope
/plugin install clickscope
```

Claude Code then asks for host, port, user, password, database and TLS. The password is
masked and goes to your operating system's secure storage — keychain on macOS, the
credential store on Windows — never to a settings file. Nothing else to configure.

This also installs the `clickhouse-query-craft` skill, which teaches the
explore → estimate → run discipline the tools are built around.

### As a standalone MCP server

```bash
claude mcp add clickscope --scope user \
  -e CLICKHOUSE_HOST=clickhouse.internal \
  -e CLICKHOUSE_USER=readonly_user \
  -e CLICKHOUSE_PASSWORD_FILE=$HOME/.config/clickscope/password \
  -e CLICKHOUSE_DATABASE=my_db \
  -- uvx --from git+https://github.com/sgr-xd/clickscope clickscope
```

Works with any MCP client, not only Claude Code — point it at
`uvx --from git+https://github.com/sgr-xd/clickscope clickscope` over stdio.

### From source

```bash
git clone https://github.com/sgr-xd/clickscope && cd clickscope
uv venv && uv pip install -e ".[dev]"
pytest -m "not integration"      # 134 tests, no database needed
```

## Configure

Only needed for the standalone and from-source paths; the plugin asks instead.

```bash
export CLICKHOUSE_HOST=clickhouse.internal
export CLICKHOUSE_PORT=8123
export CLICKHOUSE_USER=readonly_user
export CLICKHOUSE_PASSWORD_FILE=~/.config/clickscope/password   # preferred
export CLICKHOUSE_DATABASE=my_db
```

The password is read from `CLICKHOUSE_PASSWORD_FILE` if set, otherwise
`CLICKHOUSE_PASSWORD`. It is never accepted as a command-line argument — arguments leak
through shell history and `ps`.

## Several clusters

Point `CLICKSCOPE_PROFILES` at a JSON file, and every tool gains an optional `cluster`
argument.

**The file holds references, never values** — so it is safe to commit, and safe to read
over someone's shoulder:

```json
{
  "default": "prod",
  "limits": { "max_limit": 5000 },
  "clusters": {
    "prod": {
      "description": "primary analytics cluster",
      "host":     "${env:PROD_CH_HOST}",
      "port":     "${env:PROD_CH_PORT:-8123}",
      "user":     "${env:PROD_CH_USER:-readonly}",
      "password": "${cmd:vault kv get -field=password secret/ch-prod}",
      "database": "${env:PROD_CH_DB}",
      "limits":   { "default_limit": 25, "max_limit": 200 }
    },
    "staging": {
      "host":     "clickhouse-staging.example.com",
      "password": "${file:~/.config/clickscope/staging.pw}",
      "database": "events"
    }
  }
}
```

### Secret references

Any string may contain `${scheme:argument}`, resolved once at startup.

| Scheme | Example | Notes |
|---|---|---|
| `env` | `${env:PROD_CH_PASSWORD}` | `${env:VAR:-default}` supplies a fallback |
| `file` | `${file:~/.config/clickscope/prod.pw}` | Trailing newline stripped. What Kubernetes and Docker secret mounts give you |
| `cmd` | `${cmd:vault kv get -field=password secret/ch-prod}` | stdout of a command |

`cmd` is deliberately the only secret-manager integration. Depending on a cloud SDK would
serve one vendor's users and strand the rest; shelling out to the tool you already run
serves everyone:

```
${cmd:gcloud secrets versions access latest --secret=ch-prod}
${cmd:aws secretsmanager get-secret-value --secret-id ch-prod --query SecretString --output text}
${cmd:az keyvault secret show --vault-name v --name ch-prod --query value -o tsv}
${cmd:op read op://vault/ch-prod/password}
${cmd:pass show clickhouse/prod}
```

Commands run **without a shell** — the string is split and executed directly, so a value
cannot chain another command with `;` or open a subshell. `clickscope.secrets.register_scheme`
is available if you want a native backend instead.

Rules worth knowing:

- **A literal password is rejected**, with an error pointing at the reference syntax. A
  warning would not do: people commit past warnings. This is what makes "safe to commit"
  a property of the format rather than a convention.
- **A resolved value is never re-expanded.** A secret whose contents happen to look like
  `${env:OTHER}` is data, not a reference — otherwise control of one secret would grant
  reads of others.
- **A profiles file writable by anyone but its owner is refused.** A `${cmd:...}` reference
  means the file decides what gets executed, so its integrity matters more than its
  confidentiality. SSH refuses a private key on the same grounds.
- Host, port, user and database may be literals or references; only `password` is forced
  to be a reference.

### Limits and routing

Limits layer: environment defaults → file-wide `limits` → per-cluster `limits`, so
production can be stricter than staging without restating every field.

Connections are lazy — an unreachable cluster does not stop the server starting and does
not affect the others; you get a normal error payload naming the cluster.

There is deliberately **no `use_cluster` tool.** Selection is an argument on every call,
never stored state, and every response carries the `cluster` that answered it. A mode you
can forget you are in is how you read production believing you are on staging.

```
list_clusters                                    → what exists, and which is default
run_select_query(sql="…")                        → the default cluster
run_select_query(sql="…", cluster="staging")     → explicitly staging
```

The single-cluster environment variables above keep working unchanged; they simply produce
one cluster named `default`.

## Tools

**Discovery** — schema exploration, no user SQL accepted:

| Tool | Purpose |
|---|---|
| `list_clusters` | Configured clusters, which is default, and each one's limits |
| `list_databases` | Databases visible to your user |
| `list_tables` | Tables with engine, row count, size, sorting key, partition key |
| `describe_table` | Columns, types, codecs, TTL, per-column compression |
| `show_create_table` | Full `CREATE TABLE` DDL |
| `list_partitions` | Parts, rows and bytes per partition |

**Query**:

| Tool | Purpose |
|---|---|
| `run_select_query` | Execute a `SELECT` under enforced caps |
| `sample_rows` | Preview rows from a table (SQL built server-side) |

**Cost & validation** — these read no data:

| Tool | Purpose |
|---|---|
| `validate_query` | `EXPLAIN SYNTAX` — is the SQL legal? |
| `estimate_query_cost` | `EXPLAIN ESTIMATE` — parts / rows / marks it would read, as a share of the table |
| `explain_plan` | `EXPLAIN PLAN indexes=1` — how much the indexes actually pruned |

### Why this matters

Same table, two filters. The first one reads everything:

```
no key filter    verdict=full_scan   parts=13  rows=43,373  (100.0% of table)
                 → Add a filter on the partition key or the leading sorting-key column.
key + partition  verdict=selective   parts=4   rows=4,445   (10.2% of table)
```

`explain_plan` shows why, stage by stage:

```
no key filter    uses_index=False   granules 110/113 (97.3%) — effectively a full scan
key + partition  uses_index=True    granules  11/113 (9.7%)
    MinMax      parts 8/17   granules 35/113
    Partition   parts 8/8    granules 35/35
    PrimaryKey  parts 7/8    granules 11/35
```

Note that `uses_index` is derived from the ratio, not from the mere presence of an `Indexes:`
section — ClickHouse emits that section even when the index keeps 97% of the granules.

## Safety

ClickScope is read-only, enforced in three independent layers:

1. **Statement policy** — single statement only; must open with `SELECT`, `WITH…SELECT`,
   `SHOW`, `DESCRIBE` or `EXPLAIN`; DDL/DML keywords, `INTO OUTFILE` and inline `SETTINGS`
   overrides are rejected. A missing `LIMIT` is appended; an oversized one is clamped.
2. **Server-enforced caps** — every query runs with `readonly=1` plus `max_execution_time`,
   `max_result_rows`, `max_rows_to_read`, `max_bytes_to_read`, `max_memory_usage` and
   `max_threads`. These bind inside ClickHouse, so they hold even if layer 1 is bypassed.
3. **Database grants** — connect as a user with only `SELECT`. This is the layer that cannot
   be argued with, and the one you should not skip:

```sql
CREATE USER readonly_user IDENTIFIED BY '…' SETTINGS readonly = 1;
GRANT SELECT ON my_db.* TO readonly_user;
```

The policy layer **rejects, it does not rewrite** — the sole exception being `LIMIT`
injection. Query rewriting that tries to be clever about filters is deployment-specific and
fails in ways that are hard to predict, so ClickScope does not do it.

### Tuning the caps

| Variable | Default |
|---|---|
| `CLICKSCOPE_MAX_EXECUTION_TIME` | `30` (seconds) |
| `CLICKSCOPE_MAX_RESULT_ROWS` | `10000` |
| `CLICKSCOPE_MAX_ROWS_TO_READ` | `100000000` |
| `CLICKSCOPE_MAX_BYTES_TO_READ` | `10000000000` (10 GB) |
| `CLICKSCOPE_MAX_MEMORY_USAGE` | `4000000000` (4 GB) |
| `CLICKSCOPE_MAX_THREADS` | `4` |
| `CLICKSCOPE_DEFAULT_LIMIT` | `100` |
| `CLICKSCOPE_MAX_LIMIT` | `10000` |
| `CLICKSCOPE_AUDIT_LOG` | unset — set a path to record every decision as JSONL |

## License

Apache-2.0
