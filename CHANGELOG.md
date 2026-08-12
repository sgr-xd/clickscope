# Changelog

All notable changes to ClickScope. Versions follow [semantic versioning](https://semver.org).

Note that the version must be bumped for **any** shipped change, including
documentation and skills — the plugin cache is keyed by version, so an unchanged
version means installed copies silently keep the previous build. See *Releasing* in the
README.

## [0.1.4]

### Fixed

- Documentation claimed that upgrading erases the stored password and that a profiles file
  was the way to avoid it. Both were wrong: *Update now* in `/plugin` preserves all
  configuration, keychain entry included. The claim came from testing only the CLI, which
  has no update subcommand, and generalising from it.

## [0.1.3]

### Added

- `clickscope --version` and `--help`. Both answer before any configuration is read, so
  they work on a broken install — which is when you most need to know which build is
  running.
- `CLICKSCOPE_PROFILES` restored as a plugin configuration field, for deployments with
  more than one cluster or with credentials held in a secret manager.

### Fixed

- `version()` no longer shadows its own import. Unchanged behaviour, but the shadowing
  made the failure mode hard to read.

## [0.1.2]

### Added

- `clickhouse-cluster-health` skill — a one-pass sweep of replication, disks, parts,
  merges, mutations, detached parts, errors and current load, with a single verdict.
  Thanks to [@Harish-Narayan](https://github.com/Harish-Narayan).

### Changed

- `clickhouse-query-craft` narrowed to the statement being written, handing off server and
  table health to `clickhouse-cluster-health` by name. Two skills claiming "why is this
  slow" would compete for the same questions.
- Corrected two of its claims that had gone stale against the tools: `validate_query`
  builds a plan rather than running `EXPLAIN SYNTAX`, and `explain_plan` is judged by the
  granule ratio rather than by whether an `Indexes:` section appears.

## [0.1.1]

### Fixed

- `list_partitions` had never worked. It generated `sum(bytes_on_disk) AS bytes_on_disk`,
  so a later `formatReadableSize(sum(bytes_on_disk))` resolved to the alias and ClickHouse
  rejected the nested aggregate. Aliases no longer repeat a source column name.
- `validate_query` reported success for queries that could not run. `EXPLAIN SYNTAX`
  parses without resolving names, so an unknown column passed. It now builds the query
  plan, which resolves every name and still reads no data.
- A trailing `FORMAT` clause produced a syntax error. `FORMAT` must be last, so the
  injected `LIMIT` landed after it. It is now rejected with an explanation rather than
  repositioned — the wire format belongs to the driver, and these tools return JSON
  regardless.
- Blank plugin configuration values were taken literally. An untouched field arrives as
  `""` rather than absent, so the server connected as user `""` and failed authentication
  with an error pointing nowhere near the cause.
- `elapsed_ms` was always zero; the driver exposes no duration attribute, and the figure
  comes from the summary header.

### Changed

- Plugin configuration reduced from eight fields to four. Eight would not render in a
  24-row terminal, overdrawing itself into an unusable state. Host, port and TLS became a
  single `Server` field accepting `host`, `host:port` or a URL.

### Added

- `TestEveryToolAgainstLiveServer` — every tool called through `mcp.call_tool` against a
  real server. The previous integration tests ran SQL written in the test rather than the
  SQL the tools generate, which is why two tools shipped having never worked.

## [0.1.0]

Initial release: twelve read-only tools across discovery, guarded execution and cost
estimation; a policy engine that rejects rather than rewrites; secret references
(`${env:}`, `${file:}`, `${cmd:}`) so a profiles file holds no values; multi-cluster
routing by explicit per-call argument; and the `clickhouse-query-craft` skill.
