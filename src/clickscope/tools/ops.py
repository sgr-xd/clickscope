"""Operational introspection.

What the server is doing, what it did, and whether its background work is keeping up.
Every query here is written by ClickScope against `system.*` with bound parameters, so
none of it goes through the policy engine — there is no caller SQL to police.

These exist as typed tools rather than as SQL for the model to compose because the
statements are fiddly and easy to get subtly wrong: the difference between counting
`query_log` rows and counting queries is a `type != 'QueryStart'` filter, and getting it
wrong doubles every number without any sign that it happened.
"""

from __future__ import annotations

from typing import Any

from mcp.server import MCPServer

from ..connection import QueryResult
from ..registry import ClusterRegistry
from . import READ_ONLY
from ._common import resolve, run


def _rows_as_dicts(result: QueryResult) -> list[dict[str, Any]]:
    return [dict(zip(result.columns, row, strict=True)) for row in result.rows]


def register(mcp: MCPServer, registry: ClusterRegistry) -> None:
    @mcp.tool(annotations=READ_ONLY)
    def list_running_queries(cluster: str | None = None) -> dict[str, Any]:
        """Show queries executing on the server right now, longest first.

        The first thing to check when the cluster feels slow. An empty result means
        nothing is running and the problem is historical — use `search_query_log`.

        Args:
            cluster: Which configured cluster to use. Defaults to the default cluster.
        """
        handle, err = resolve(registry, cluster)
        if err:
            return err
        result, qerr = run(
            handle,
            """
            SELECT
                query_id,
                user,
                round(elapsed, 1) AS elapsed_s,
                formatReadableSize(memory_usage) AS memory,
                read_rows,
                formatReadableSize(read_bytes) AS read_bytes,
                substring(query, 1, 200) AS query
            FROM system.processes
            ORDER BY elapsed DESC
            """,
        )
        if qerr:
            return qerr
        return {
            "cluster": handle.name,
            "running": _rows_as_dicts(result),
            "count": result.row_count,
        }

    @mcp.tool(annotations=READ_ONLY)
    def search_query_log(
        hours: int = 24,
        failed_only: bool = False,
        min_duration_ms: int = 0,
        limit: int = 20,
        cluster: str | None = None,
    ) -> dict[str, Any]:
        """Search completed queries: the slowest, or the ones that failed.

        Use to find what made the cluster slow, or to see what a query actually cost
        after it ran. With `failed_only`, results are grouped by exception instead, which
        is far more useful than reading individual failures.

        Args:
            hours: How far back to look. Keep this tight — query_log is a large table.
            failed_only: Group failures by exception rather than listing slow queries.
            min_duration_ms: Ignore queries faster than this.
            limit: Maximum rows to return.
            cluster: Which configured cluster to use. Defaults to the default cluster.
        """
        handle, err = resolve(registry, cluster)
        if err:
            return err
        params = {
            "hours": max(1, min(hours, 24 * 30)),
            "lim": max(1, min(limit, 200)),
            "min_ms": max(0, min_duration_ms),
        }

        if failed_only:
            # Grouping by code turns a wall of individual failures into a short list of
            # causes, which is what you actually want to act on.
            sql = """
                SELECT
                    exception_code,
                    any(exception) AS example,
                    count() AS occurrences,
                    max(event_time) AS last_seen
                FROM system.query_log
                WHERE event_time >= now() - toIntervalHour({hours:UInt32})
                  AND type IN ('ExceptionBeforeStart', 'ExceptionWhileProcessing')
                GROUP BY exception_code
                ORDER BY occurrences DESC
                LIMIT {lim:UInt32}
            """
        else:
            # `type != 'QueryStart'` keeps each query to a single completed row; without
            # it every query is counted twice with half the numbers missing.
            sql = """
                SELECT
                    event_time,
                    query_duration_ms,
                    formatReadableSize(memory_usage) AS memory,
                    read_rows,
                    formatReadableSize(read_bytes) AS read_bytes,
                    result_rows,
                    substring(query, 1, 200) AS query
                FROM system.query_log
                WHERE event_time >= now() - toIntervalHour({hours:UInt32})
                  AND type != 'QueryStart'
                  AND query_duration_ms >= {min_ms:UInt64}
                ORDER BY query_duration_ms DESC
                LIMIT {lim:UInt32}
            """

        result, qerr = run(handle, sql, params)
        if qerr:
            return qerr
        return {
            "cluster": handle.name,
            "hours": params["hours"],
            "grouped_by_exception": failed_only,
            "entries": _rows_as_dicts(result),
            "count": result.row_count,
        }

    @mcp.tool(annotations=READ_ONLY)
    def table_storage_stats(
        database: str | None = None, limit: int = 20, cluster: str | None = None
    ) -> dict[str, Any]:
        """Rank tables by part count and size, to find where storage pressure is.

        A high `parts` count is the usual cause of a table that has quietly become slow:
        each part is read separately, so part count drives cost as much as row count. A
        few thousand active parts means inserts are arriving faster, and smaller, than
        merges can combine them — a write-side problem, not a query-side one.

        Args:
            database: Restrict to one database. Omit to cover them all.
            limit: Maximum tables to return.
            cluster: Which configured cluster to use. Defaults to the default cluster.
        """
        handle, err = resolve(registry, cluster)
        if err:
            return err
        result, qerr = run(
            handle,
            """
            SELECT
                database,
                table,
                count() AS parts,
                sum(rows) AS total_rows,
                formatReadableSize(sum(bytes_on_disk)) AS size,
                formatReadableSize(sum(data_uncompressed_bytes)) AS uncompressed,
                round(sum(data_uncompressed_bytes) / nullIf(sum(bytes_on_disk), 0), 2)
                    AS compression_ratio,
                max(modification_time) AS last_modified
            FROM system.parts
            WHERE active
              AND (isNull({db:Nullable(String)}) OR database = {db:Nullable(String)})
            GROUP BY database, table
            ORDER BY parts DESC, sum(bytes_on_disk) DESC
            LIMIT {lim:UInt32}
            """,
            {"db": database, "lim": max(1, min(limit, 200))},
        )
        if qerr:
            return qerr
        return {
            "cluster": handle.name,
            "tables": _rows_as_dicts(result),
            "count": result.row_count,
        }

    @mcp.tool(annotations=READ_ONLY)
    def background_operations(cluster: str | None = None) -> dict[str, Any]:
        """Report merges in flight and mutations that have not finished.

        Merges and mutations are the server's background work, and both fail quietly.
        A long merge over many parts explains slow reads and rising disk use. A mutation
        with a `latest_fail_reason` is stuck: it retries forever, blocks the mutations
        queued behind it, and nothing surfaces it unless you look.

        Args:
            cluster: Which configured cluster to use. Defaults to the default cluster.
        """
        handle, err = resolve(registry, cluster)
        if err:
            return err

        merges, qerr = run(
            handle,
            """
            SELECT
                database,
                table,
                round(elapsed, 1) AS elapsed_s,
                round(progress, 3) AS progress,
                num_parts,
                formatReadableSize(total_size_bytes_compressed) AS size,
                formatReadableSize(memory_usage) AS memory
            FROM system.merges
            ORDER BY elapsed DESC
            """,
        )
        if qerr:
            return qerr

        mutations, qerr = run(
            handle,
            """
            SELECT
                database,
                table,
                mutation_id,
                substring(command, 1, 160) AS command,
                create_time,
                parts_to_do,
                latest_fail_reason
            FROM system.mutations
            WHERE NOT is_done
            ORDER BY create_time
            LIMIT 50
            """,
        )
        if qerr:
            return qerr

        unfinished = _rows_as_dicts(mutations)
        stuck = [m for m in unfinished if m.get("latest_fail_reason")]
        return {
            "cluster": handle.name,
            "merges_in_flight": _rows_as_dicts(merges),
            "unfinished_mutations": unfinished,
            "stuck_mutations": len(stuck),
            "assessment": (
                f"{len(stuck)} mutation(s) failing and retrying — these will not clear on their own"
                if stuck
                else "no failing mutations"
            ),
        }

    @mcp.tool(annotations=READ_ONLY)
    def replication_status(
        unhealthy_only: bool = True, cluster: str | None = None
    ) -> dict[str, Any]:
        """Report replica health: read-only replicas, lag, and queue backlog.

        `is_readonly` usually means a replica lost its coordination session. Writes are
        refused while reads carry on, so this can run for a long time before anyone
        notices. With `unhealthy_only`, an empty result is the healthy answer.

        Args:
            unhealthy_only: Only return replicas that are read-only, lagging or backed up.
            cluster: Which configured cluster to use. Defaults to the default cluster.
        """
        handle, err = resolve(registry, cluster)
        if err:
            return err
        condition = (
            "WHERE is_readonly OR is_session_expired OR absolute_delay > 60 OR queue_size > 100"
            if unhealthy_only
            else ""
        )
        result, qerr = run(
            handle,
            f"""
            SELECT
                database,
                table,
                is_readonly,
                is_session_expired,
                absolute_delay,
                queue_size,
                inserts_in_queue,
                merges_in_queue,
                total_replicas,
                active_replicas
            FROM system.replicas
            {condition}
            ORDER BY absolute_delay DESC, queue_size DESC
            LIMIT 100
            """,
        )
        if qerr:
            return qerr
        rows = _rows_as_dicts(result)
        return {
            "cluster": handle.name,
            "replicas": rows,
            "count": len(rows),
            "assessment": (
                "no unhealthy replicas"
                if unhealthy_only and not rows
                else f"{len(rows)} replica(s) reported"
            ),
        }
