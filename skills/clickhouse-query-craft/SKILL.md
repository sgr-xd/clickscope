---
name: clickhouse-query-craft
description: Write ClickHouse queries that stay cheap, and check what one will cost before running it. Use when composing, reviewing or debugging a specific SQL statement — choosing filters, a time range, an ORDER BY key or a partition; working out why one query reads far more data than expected; or reading EXPLAIN ESTIMATE / EXPLAIN PLAN output to decide whether a statement is safe to run against a large table. Does NOT cover the health of the server or a table — stuck merges, failing mutations, replication lag, part accumulation, or what is running right now: use clickhouse-cluster-health for those.
license: Apache-2.0
metadata:
  version: "1.0.0"
---

# ClickHouse query craft

ClickHouse will happily read a billion rows to answer a question that needed a thousand.
It rarely errors — it just gets slow and expensive. So the discipline is to find out what a
query will read *before* running it.

## The loop

**Explore → estimate → fix → run.** Skipping the estimate is the mistake this skill exists
to prevent.

1. **`list_tables`** — read `sorting_key` and `partition_key`. These decide everything about
   cost. Note `total_rows` so you know what "expensive" means for this table.
2. **`estimate_query_cost`** — reports parts, rows and marks the query would read, and what
   share of the table that is. Nothing is read to produce this.
3. If the verdict is **`full_scan`** or **`heavy`**, fix the query (below) and estimate
   again.
4. **`run_select_query`** once the verdict is `selective` — or once you have accepted that a
   scan is genuinely what you need.

Use `validate_query` when you are unsure the SQL is even legal. It builds the query plan,
so it resolves table and column names as well as syntax, and still reads no data — never
validate by running a query with `LIMIT 1`.

## What makes a query cheap

**Filter on the leading column of the sorting key.** ClickHouse stores rows sorted by
`ORDER BY` and keeps a sparse index over it. A filter on the first column lets it skip whole
granules. A filter on the *third* column, with no constraint on the first two, usually cannot
skip anything.

```sql
-- ORDER BY (tenant_id, timestamp)
WHERE tenant_id = 'acme' AND timestamp >= now() - INTERVAL 1 DAY   -- skips almost everything
WHERE timestamp >= now() - INTERVAL 1 DAY                          -- reads every tenant
```

**Pin the partition.** If `partition_key` is `toYYYYMM(timestamp)`, a bounded `timestamp`
range prunes to a few partitions. An unbounded one reads all of them. Use `list_partitions`
to see what exists before guessing at a range.

**Always bound time.** On an events table this is usually the single biggest lever. Prefer an
explicit range over an open-ended comparison.

**Select the columns you need.** ClickHouse is columnar: `SELECT *` on a 90-column table
reads 90 columns off disk. Naming five columns reads five.

**Filter early with `PREWHERE`** when one condition is highly selective and cheap to
evaluate. ClickHouse often does this automatically, but it is worth being explicit when a
narrow filter guards an expensive one.

## What makes a query expensive

**`FINAL`.** It forces a merge at read time, and it does not scale. Prefer aggregating to
deduplicate — `argMax(col, version)` grouped by the key, or a `LIMIT 1 BY key` with an
ordering. Reach for `FINAL` only on a small, bounded result.

**Unbounded `JOIN`.** ClickHouse builds the right-hand side in memory. Filter *both* sides
before joining, and put the smaller table on the right. A join whose right side is a whole
fact table will exhaust memory.

**`GROUP BY` on a high-cardinality expression.** Grouping by a raw string column with
millions of distinct values costs far more than grouping by a `LowCardinality` column or an
integer id.

**Functions applied to the indexed column.** `WHERE toDate(timestamp) = today()` prevents
index use, because the index is on `timestamp`, not on `toDate(timestamp)`. Write the range
against the raw column instead: `WHERE timestamp >= today() AND timestamp < today() + 1`.

## Reading the output

**`estimate_query_cost`** gives you, per table: `parts`, `rows`, `marks`, `table_total_rows`
and `fraction_of_table`.

- `fraction_of_table` near `1.0` — no index pruning is happening. Your filter does not match
  the sorting key, or a function is wrapping the indexed column.
- `parts` equal to the table's total part count — no partition pruning. Add or tighten the
  time bound.
- A small `fraction_of_table` — the index is working. Run it.

**`explain_plan`** shows why, as `granules_selected` against `granules_total` plus a
stage-by-stage breakdown. Judge it by that ratio, not by whether the plan mentions indexes:
ClickHouse prints an `Indexes:` section even when it kept 97% of the granules, which is a
full scan wearing a disguise.

Estimates come from part metadata and are approximate — treat them as an order of magnitude,
not a promise. After running, compare against the `rows_read` the query actually reported.

## More than one cluster

Call `list_clusters` first. If it returns more than one, pass `cluster` explicitly on every
call rather than relying on the default — the caps differ per cluster, and so does the data.
There is no "current" cluster to switch to; the argument is the only thing that decides where
a query goes. Every response tells you which cluster answered it, so check that field when a
result looks unfamiliar.

## Notes on this connection

Every tool here is read-only, and the server applies row, byte, memory and time caps to each
query. A missing `LIMIT` is added automatically and an oversized one is clamped — check
`executed_sql` in the response to see what actually ran, and `policy_notes` for what changed.

A `truncated: true` in the result means you hit the limit and there are more rows. Aggregate
rather than paginating: `count()`, `GROUP BY`, or a narrower filter almost always answers the
question better than fetching page two.
