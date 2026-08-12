---
name: clickhouse-cluster-health
description: Run a one-shot health check across a ClickHouse deployment and report what is wrong. Use when asked how the cluster is doing, whether it is healthy, why it feels slow, or to check replication lag, replica readonly state, disk space, part counts, stuck merges or mutations, detached parts, recent errors or long-running queries. Also use before and after a migration, node addition or version upgrade.
license: Apache-2.0
metadata:
  version: "1.0.0"
---

# ClickHouse cluster health

One pass, every check, one verdict. This skill answers "is the cluster healthy right now" —
it holds no state, keeps no history, and schedules nothing. Everything it needs comes from
system tables in a single run.

## How to run it

**1. Find out what you are talking to.** Call `list_clusters` first. Everything below runs
once *per cluster entry*, passing `cluster=` explicitly on every call — never rely on the
default. A single-node setup returns one entry named `default`; that is the normal case and
the checks are unchanged.

Fan-out matters: a cluster's health is mostly a question of whether its nodes *agree*, and
you cannot see disagreement from one node. If the deployment has several nodes but only one
profile is configured, say so in the report — you are checking one node, not the cluster, and
the difference is worth stating rather than glossing.

**2. Run the checks below**, in order. Each is a single read-only statement against system
tables.

**3. Report** using the format at the end.

Do not stop at the first problem. A stuck merge and a full disk are usually the same story,
and you can only tell by having both.

### These queries do not need costing

The sibling `clickhouse-query-craft` skill teaches explore → estimate → run. That discipline
is for user tables. Everything here reads system tables — in-memory server metadata, already
bounded, no granules to prune. Running `estimate_query_cost` on them wastes a call and tells
you nothing. Run these directly.

Two constraints do still apply. A missing `LIMIT` is appended, so **aggregate rather than
listing** — the queries below already do. And if a response comes back `truncated: true`,
report that you saw a partial answer rather than drawing a conclusion from it.

## The checks

### 1. Topology and node agreement

```sql
SELECT cluster, shard_num, replica_num, host_name, port, is_local, errors_count
FROM system.clusters
ORDER BY cluster, shard_num, replica_num
```

Run against **two different nodes** if you have them. A node's view of the cluster comes from
its own configuration, so two nodes disagreeing about shard or replica membership is a
split-brain configuration — one of them is working from a stale or wrong config file. This is
the one check whose entire value is in the comparison; from a single node it tells you almost
nothing.

Non-zero `errors_count` against a host means this node has recently failed to reach it.

### 2. Replica health

```sql
SELECT database, table, is_readonly, is_session_expired, absolute_delay,
       queue_size, inserts_in_queue, merges_in_queue,
       active_replicas, total_replicas
FROM system.replicas
WHERE is_readonly OR is_session_expired OR absolute_delay > 0 OR queue_size > 0
ORDER BY absolute_delay DESC, queue_size DESC
```

The `WHERE` keeps the result to tables that have something to say — a healthy cluster returns
zero rows here, which is the answer you want.

- `is_readonly` or `is_session_expired` — the replica has lost its ZooKeeper/Keeper session
  and **has stopped accepting writes**. Serious regardless of any other number.
- `absolute_delay` — seconds behind the leader. Over 60s is worth reporting; over 300s is
  serious. A delay that is large *and* growing between two adjacent checks is worse than a
  large steady one.
- `active_replicas < total_replicas` — a replica is down. Say which table.

### 3. Replication queue failures

```sql
SELECT database, table, type, count() AS entries, max(num_tries) AS max_tries,
       any(last_exception) AS sample_exception
FROM system.replication_queue
WHERE num_tries > 1
GROUP BY database, table, type
ORDER BY max_tries DESC
LIMIT 50
```

A queue that is deep but advancing is a busy cluster. A queue with entries retried many times
is a *stuck* cluster — the same task failing repeatedly. `max_tries` in the hundreds means it
will not clear on its own.

### 4. Disk space

```sql
SELECT name, path, free_space, total_space,
       round(1 - free_space / total_space, 4) AS used_ratio
FROM system.disks
ORDER BY used_ratio DESC
```

Over 0.80 is worth reporting; over 0.90 is serious — ClickHouse needs free space to merge, so
a full disk stops merges, which grows part counts, which degrades reads. That cascade is why
this check outranks its apparent importance.

**Compare replicas of the same shard against each other.** They hold the same data and should
sit at similar `used_ratio`. A meaningful gap between two replicas of one shard means they
have diverged — different retention, a failed merge on one side, or disks that are not the
size you think. Do not compare *across* shards: different shards legitimately hold different
volumes, and on many deployments the disks are genuinely different sizes.

### 5. Part pressure

```sql
SELECT database, table, count() AS active_parts, sum(bytes_on_disk) AS bytes,
       sum(rows) AS rows
FROM system.parts
WHERE active
GROUP BY database, table
ORDER BY active_parts DESC
LIMIT 30
```

Part count is the clearest early indicator that merges are losing to ingestion. A few hundred
active parts on one table is normal for a busy table; several thousand means merges are behind
and reads are getting slower. If you also see `TOO_MANY_PARTS` in check 8, ingestion is already
being throttled and this is the cause.

### 6. Merges and mutations

```sql
SELECT database, table, count() AS merges, max(elapsed) AS longest_elapsed_s,
       min(progress) AS least_progress
FROM system.merges
GROUP BY database, table
ORDER BY longest_elapsed_s DESC
LIMIT 30
```

```sql
SELECT database, table, count() AS pending, min(create_time) AS oldest,
       any(latest_fail_reason) AS sample_failure
FROM system.mutations
WHERE NOT is_done
GROUP BY database, table
ORDER BY oldest ASC
LIMIT 30
```

A merge running for hours with `progress` barely above zero is stuck, not slow. For mutations,
age is the signal: anything pending more than a few hours is not going to finish by itself, and
a non-empty `latest_fail_reason` says why. More than ~20 pending on one table is a backlog.

### 7. Detached parts

```sql
SELECT database, table, reason, count() AS parts
FROM system.detached_parts
GROUP BY database, table, reason
ORDER BY parts DESC
LIMIT 30
```

Any row here is data the server has set aside and is **not serving**. Often the quiet
explanation for "rows are missing". A healthy cluster returns nothing.

### 8. Errors

```sql
SELECT name, code, value, last_error_time
FROM system.errors
WHERE value > 0
ORDER BY last_error_time DESC
LIMIT 40
```

These counters are **cumulative since server start**, so a non-zero value is not by itself a
problem — a server up for a year will have accumulated some. What matters is `last_error_time`
being recent. Without history you cannot compute a rate, so report *what* is erroring and *how
recently*, and resist turning a raw count into a severity.

Then recent query failures:

```sql
SELECT exception_code, any(exception) AS sample, count() AS n,
       countIf(query_kind = 'Insert') AS insert_failures
FROM system.query_log
WHERE type = 'ExceptionWhileProcessing'
  AND event_time >= now() - INTERVAL 3 HOUR
GROUP BY exception_code
ORDER BY n DESC
LIMIT 30
```

Weigh `insert_failures` more heavily than read failures: a failed `SELECT` is retried by
someone, a failed `INSERT` is usually data that never arrives. Watch specifically for
`TOO_MANY_PARTS` (merges losing to ingestion — cross-check 5), `MEMORY_LIMIT_EXCEEDED`, and
`NO_FREE_SPACE`.

If `system.query_log` does not exist, query logging is disabled on this server. Say so and
move on — it is a configuration choice, not a fault.

### 9. Load right now

```sql
SELECT query_id, user, elapsed, memory_usage, left(query, 120) AS query_sample
FROM system.processes
ORDER BY elapsed DESC
LIMIT 20
```

Anything over 60s deserves a mention; over 300s, name it explicitly with its `query_id` so
someone can act on it. Note that this is a point-in-time sample — a quiet result means nothing
was running at this instant, not that the cluster is idle.

### 10. Host resources

```sql
SELECT metric, value
FROM system.asynchronous_metrics
WHERE metric IN ('LoadAverage1', 'OSMemoryAvailable', 'OSMemoryTotal',
                 'OSUserTimeNormalized', 'OSSystemTimeNormalized')
ORDER BY metric
```

The normalized CPU metrics are per-core fractions, so ~1.0 means saturated. `OSMemoryAvailable`
approaching zero explains `MEMORY_LIMIT_EXCEEDED` in check 8. These are only meaningful when
ClickHouse is the main process on the host; if it shares the box, say so rather than reading
them as ClickHouse's own usage.

## Reporting

Lead with the verdict, then the evidence. Someone should be able to stop reading after the
first line and still know whether to worry.

```
ClickHouse health — <N> node(s) checked, <UTC timestamp>

VERDICT: healthy | degraded | serious

Serious
  • <finding, with the number that makes it serious and the table/node it is on>

Worth watching
  • <finding>

Checked and clean
  replicas · disks · parts · merges · mutations · detached · errors · load
```

Rules for the report:

- **Name the node.** Every response carries the `cluster` that answered it. A finding without
  a node attached is not actionable.
- **Quote the number.** "Replica delay 412s on `events`" beats "replication is lagging".
- **List what was clean.** The absence of findings is a result, and without it the reader
  cannot tell a healthy cluster from a check that failed to run.
- **Say what you could not check.** A missing `query_log`, an unreachable node, a
  `truncated: true` response — all of it goes in the report. Silence reads as "fine".
- **Connect findings that share a cause** rather than listing them as separate bullets. Full
  disk → stalled merges → rising parts → `TOO_MANY_PARTS` on insert is one incident, and
  reporting it as four is how the actual cause gets missed.
- **Do not diagnose past the evidence.** These checks show *what* is wrong. Why it started
  usually needs history this skill does not have. Offer the hypothesis, label it as one.

## What this skill does not do

- **It does not fix anything.** Every tool available here is read-only, and remediation —
  `SYSTEM RESTART REPLICA`, dropping a partition, detaching a table — is a human decision made
  with a write-capable connection this server deliberately does not have. Recommend the action,
  never attempt it.
- **It does not compare against history.** No previous run to diff against, so "growing",
  "since yesterday" and "new" are not claims you can make from one pass. If a trend matters,
  say what a second run would settle.
- **It does not check ACLs.** `system.users`, `system.roles`, `system.row_policies` and
  `system.settings_profiles` are blocked by ClickScope policy, so user, role and row-policy
  parity across nodes is out of scope here — a real gap if you are checking a node that was
  added recently, and one worth closing by hand.
