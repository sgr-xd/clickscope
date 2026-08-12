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

While the repository is private, use a local checkout instead — `/plugin marketplace add /path/to/clickscope` — or the standalone route below.

Claude Code then asks for four things:

| Field | Example |
|---|---|
| **Server** | `clickhouse.internal`, `host:port`, or `https://abc.clickhouse.cloud` |
| **Username** | `readonly_user` |
| **Password** | masked; goes to your operating system's secure storage — keychain on macOS, the credential store on Windows — never to a settings file |
| **Database** | `my_db` |

Port and TLS come from whatever you put in Server, so there is nothing else to set.

This also installs two skills: `clickhouse-query-craft`, which teaches the
explore → estimate → run discipline the tools are built around, and
`clickhouse-cluster-health`, a one-shot sweep of replication, disks, parts, merges,
mutations and errors that reports whether the cluster is healthy right now.

### As a standalone MCP server

```bash
claude mcp add clickscope --scope user \
  -e CLICKHOUSE_HOST=clickhouse.internal \
  -e CLICKHOUSE_USER=readonly_user \
  -e CLICKHOUSE_PASSWORD_FILE=$HOME/.config/clickscope/password \
  -e CLICKHOUSE_DATABASE=my_db \
  -- uvx clickscope
```

Works with any MCP client, not only Claude Code — point it at `uvx clickscope`
over stdio.

### From PyPI

```bash
pip install clickscope    # or: uvx clickscope
```

### From source

```bash
git clone https://github.com/sgr-xd/clickscope && cd clickscope
uv venv && uv pip install -e ".[dev]"
pytest -m "not integration"      # unit tests, no database needed
pytest                           # adds live tests against a real server
```

## Configure

Only needed for the standalone and from-source paths; the plugin asks instead.

```bash
export CLICKHOUSE_HOST=clickhouse.internal          # or host:port, or a full URL
export CLICKHOUSE_USER=readonly_user
export CLICKHOUSE_PASSWORD_FILE=~/.config/clickscope/password   # preferred
export CLICKHOUSE_DATABASE=my_db
```

`CLICKHOUSE_HOST` accepts three forms, so port and TLS usually need no separate setting:

| You enter | Host | Port | HTTPS |
|---|---|---|---|
| `clickhouse.internal` | clickhouse.internal | 8123 | no |
| `clickhouse.internal:9440` | clickhouse.internal | 9440 | no |
| `https://abc.clickhouse.cloud:8443` | abc.clickhouse.cloud | 8443 | yes |
| `https://abc.clickhouse.cloud` | abc.clickhouse.cloud | 8443 | yes |

`CLICKHOUSE_PORT` and `CLICKHOUSE_SECURE` still work and take precedence if you set them.

The password is read from `CLICKHOUSE_PASSWORD_FILE` if set, otherwise
`CLICKHOUSE_PASSWORD`. It is never accepted as a command-line argument — arguments leak
through shell history and `ps`.

Optional, environment only (not offered in the plugin dialog, to keep it to four fields):

| Variable | Purpose |
|---|---|
| `CLICKSCOPE_PROFILES` | Path to a multi-cluster profiles file — see below |
| `CLICKSCOPE_AUDIT_LOG` | Path to a JSONL record of every query decision |

## Using it

Ask in plain language — the tools are named in the responses, not in your question.

**Learn the shape of a table first.**

> *What are the biggest tables in my database?*

`list_tables` returns each table with its `sorting_key` and `partition_key`. Those two
fields decide everything about cost: a filter matching the leading sorting-key column, or
pinning a partition, is the difference between reading a few granules and reading the lot.

**Then cost the query before running it.**

> *How many events belong to tenant acme-corp since August 1st?*

`estimate_query_cost` reports what the query *would* read, without reading it:

```
verdict=selective   parts=4   rows=4,445 of 43,374   (10.2% of table)
```

Then it runs. Compare that with a filter that misses the key:

```
verdict=full_scan   parts=13  rows=43,373 of 43,374  (100.0% of table)
   → Add a filter on the partition key or the leading sorting-key column.
```

Same table, same row count returned — 10× the work. The verdict arrives *before* the query
runs, which is the entire point: an agent can correct itself instead of discovering the cost
afterwards.

**When something is expensive, ask why.**

> *Why is that query slow?*

`explain_plan` measures how much the indexes actually pruned, stage by stage:

```
uses_index=False   granules 110/113 (97.3%) — effectively a full scan
    MinMax      parts 17/17   granules 113/113
    Partition   parts 17/17   granules 113/113
    PrimaryKey  parts 14/17   granules 110/113
```

Note `uses_index` comes from the ratio, not from the presence of an `Indexes:` section —
ClickHouse emits that section even when the index removed nothing.

**Writes simply are not available.**

> *Drop the events table*

```
Query rejected by ClickScope policy:
  - query must begin with one of SELECT, WITH, SHOW, DESC, DESCRIBE, EXPLAIN; got DROP
  - forbidden keyword: DROP
```

Rejected before any network call.

**What comes back.** Every response names the cluster that answered and the SQL actually
executed, so a rewritten `LIMIT` is visible rather than silent:

```json
{"cluster": "prod",
 "executed_sql": "SELECT tenant_id FROM events WHERE tenant_id = 'acme-corp' LIMIT 100",
 "row_count": 100, "rows_read": 9845, "elapsed_ms": 62,
 "policy_notes": ["no LIMIT present; appended LIMIT 100",
                  "result reached the limit of 100; there may be more rows"]}
```

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

## Releasing

Changes are recorded in [CHANGELOG.md](CHANGELOG.md).

**Always bump the version when shipping code**, documentation and skills included. Not doing so is silent: users keep running
the old build, and nothing reports an error to reveal it.

Three files carry the version, and they must agree:

```
.claude-plugin/plugin.json       "version"
.claude-plugin/marketplace.json  plugins[0].version
pyproject.toml                   version
```

Then:

```bash
claude plugin marketplace update clickscope
claude plugin uninstall clickscope@clickscope
claude plugin install clickscope@clickscope --scope user \
  --config CLICKHOUSE_HOST=... --config CLICKHOUSE_USER=... --config CLICKHOUSE_DATABASE=...
# re-enter the password via /plugin configure, then restart the client
```

### Why each step is there

**`marketplace update` alone does nothing to the installed copy.** The plugin cache lives at
`plugins/cache/<marketplace>/<plugin>/<version>/`. With the version unchanged the installer
considers the cached copy current, so a successful "update" can leave the old source in place.
The version *is* the cache key.

**Uninstalling erases the configuration.** `pluginConfigs` is emptied, so the reinstall passes
the non-sensitive values back with `--config` and only the password needs re-entering. Nothing
warns you about this beforehand.

**Directory-sourced installs need their build cache cleared too.** When a marketplace points at
a local directory, `${CLAUDE_PLUGIN_ROOT}` is a path that never changes between versions, so
`uvx` reuses the environment it built the first time — a fix committed hours earlier can still
reproduce, and `/reload-plugins` does not help (it starts a new process against the same stale
environment, and leaves the old one running). To confirm what a running server actually
imports:

```bash
ps -eo pid,command | grep clickscope     # note the archive-v0/<hash>/bin/python path
grep -r "the-fix-you-expect" <that env>/lib/python3.*/site-packages/clickscope/
```

The fix is `uv cache clean clickscope`, kill the server processes, then reload. Installs from
GitHub are immune, because their plugin root is version-scoped and a bump forces a rebuild.

### Before publishing

- `pytest` — the full suite, including live tests against a real server
- `clickscope --version` — confirms which build a given install is actually running,
  which is the quickest way to catch a stale plugin cache
- Claim the name on PyPI if you intend to use `uvx clickscope` in the docs; an unclaimed name
  can be taken by anyone once the repository is public

## License

Apache-2.0
