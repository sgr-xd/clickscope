"""Schema discovery.

Every query here is written by ClickScope and takes only bound parameters — no caller
string ever reaches the SQL text. That is why these tools do not go through the policy
engine: there is no user SQL to police.
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
    def list_clusters() -> dict[str, Any]:
        """List the ClickHouse clusters this server can reach.

        Every other tool takes an optional `cluster` argument naming one of these. Pass
        it explicitly whenever more than one is configured — the default is only a
        convenience, and reading the wrong environment is a real mistake.
        """
        return {
            "clusters": registry.describe_all(),
            "default": registry.default_name,
        }

    @mcp.tool(annotations=READ_ONLY)
    def list_databases(cluster: str | None = None) -> dict[str, Any]:
        """List databases visible to the connected user.

        Start here when you do not know what is on the cluster.

        Args:
            cluster: Which configured cluster to use. Defaults to the default cluster.
        """
        handle, err = resolve(registry, cluster)
        if err:
            return err
        result, qerr = run(
            handle,
            """
            SELECT name, engine
            FROM system.databases
            WHERE name NOT IN ('INFORMATION_SCHEMA', 'information_schema')
            ORDER BY name
            """,
        )
        if qerr:
            return qerr
        return {
            "cluster": handle.name,
            "databases": _rows_as_dicts(result),
            "count": result.row_count,
        }

    @mcp.tool(annotations=READ_ONLY)
    def list_tables(
        database: str | None = None,
        name_like: str | None = None,
        cluster: str | None = None,
    ) -> dict[str, Any]:
        """List tables with the facts that determine query cost.

        `sorting_key` and `partition_key` are the two things worth reading closely: a
        filter that matches the leading column of the sorting key, or that pins a
        partition, is the difference between reading a few marks and reading the table.

        Args:
            database: Restrict to one database. Defaults to the cluster's database.
            name_like: Optional SQL LIKE pattern on the table name, e.g. 'events%'.
            cluster: Which configured cluster to use. Defaults to the default cluster.
        """
        handle, err = resolve(registry, cluster)
        if err:
            return err
        db = database or handle.config.database
        result, qerr = run(
            handle,
            """
            SELECT
                name,
                engine,
                total_rows,
                total_bytes,
                formatReadableSize(total_bytes) AS size,
                sorting_key,
                partition_key,
                primary_key,
                comment
            FROM system.tables
            WHERE database = {db:String}
              AND (isNull({pattern:Nullable(String)}) OR name LIKE {pattern:Nullable(String)})
            ORDER BY total_bytes DESC, name
            """,
            parameters={"db": db, "pattern": name_like},
        )
        if qerr:
            return qerr
        return {
            "cluster": handle.name,
            "database": db,
            "tables": _rows_as_dicts(result),
            "count": result.row_count,
        }

    @mcp.tool(annotations=READ_ONLY)
    def describe_table(
        table: str, database: str | None = None, cluster: str | None = None
    ) -> dict[str, Any]:
        """Describe a table's columns, including how well each one compresses.

        `is_in_primary_key` / `is_in_sorting_key` / `is_in_partition_key` tell you which
        columns make a filter cheap. A column with a poor compression ratio and a large
        compressed size is usually the one making your queries slow.

        Args:
            table: Table name.
            database: Defaults to the cluster's database.
            cluster: Which configured cluster to use. Defaults to the default cluster.
        """
        handle, err = resolve(registry, cluster)
        if err:
            return err
        db = database or handle.config.database
        result, qerr = run(
            handle,
            """
            SELECT
                name,
                type,
                default_kind,
                default_expression,
                compression_codec,
                is_in_partition_key,
                is_in_sorting_key,
                is_in_primary_key,
                data_compressed_bytes,
                data_uncompressed_bytes,
                formatReadableSize(data_compressed_bytes) AS compressed,
                round(data_uncompressed_bytes / nullIf(data_compressed_bytes, 0), 2)
                    AS compression_ratio,
                comment
            FROM system.columns
            WHERE database = {db:String} AND table = {tbl:String}
            ORDER BY position
            """,
            parameters={"db": db, "tbl": table},
        )
        if qerr:
            return qerr
        if result.row_count == 0:
            return {
                "cluster": handle.name,
                "database": db,
                "table": table,
                "columns": [],
                "error": f"table {db}.{table} not found on {handle.name}, or not visible",
            }
        return {
            "cluster": handle.name,
            "database": db,
            "table": table,
            "columns": _rows_as_dicts(result),
            "column_count": result.row_count,
        }

    @mcp.tool(annotations=READ_ONLY)
    def show_create_table(
        table: str, database: str | None = None, cluster: str | None = None
    ) -> dict[str, Any]:
        """Return the full CREATE TABLE statement.

        Read this when you need the engine parameters, TTL rules or codecs that the
        column view does not show.

        Args:
            table: Table name.
            database: Defaults to the cluster's database.
            cluster: Which configured cluster to use. Defaults to the default cluster.
        """
        handle, err = resolve(registry, cluster)
        if err:
            return err
        db = database or handle.config.database
        # Read the DDL out of system.tables rather than running SHOW CREATE TABLE, so
        # the table name stays a bound parameter instead of being spliced into SQL.
        result, qerr = run(
            handle,
            """
            SELECT create_table_query
            FROM system.tables
            WHERE database = {db:String} AND name = {tbl:String}
            """,
            parameters={"db": db, "tbl": table},
        )
        if qerr:
            return qerr
        if result.row_count == 0:
            return {
                "cluster": handle.name,
                "database": db,
                "table": table,
                "error": f"table {db}.{table} not found on {handle.name}",
            }
        return {
            "cluster": handle.name,
            "database": db,
            "table": table,
            "ddl": result.rows[0][0],
        }

    @mcp.tool(annotations=READ_ONLY)
    def list_partitions(
        table: str,
        database: str | None = None,
        limit: int = 50,
        cluster: str | None = None,
    ) -> dict[str, Any]:
        """List a table's partitions, newest first, with rows and bytes in each.

        Use this to pick a partition to scope a query to, and to spot a table whose
        parts are not merging (a high `parts` count on one partition).

        Args:
            table: Table name.
            database: Defaults to the cluster's database.
            limit: Maximum partitions to return.
            cluster: Which configured cluster to use. Defaults to the default cluster.
        """
        handle, err = resolve(registry, cluster)
        if err:
            return err
        db = database or handle.config.database
        result, qerr = run(
            handle,
            """
            SELECT
                partition,
                count() AS parts,
                -- Aliases must not repeat a source column name: `sum(x) AS x` makes a
                -- later `sum(x)` resolve to the alias, which ClickHouse rejects as a
                -- nested aggregate (ILLEGAL_AGGREGATION).
                sum(rows) AS total_rows,
                sum(bytes_on_disk) AS total_bytes,
                formatReadableSize(sum(bytes_on_disk)) AS size,
                min(min_time) AS min_time,
                max(max_time) AS max_time
            FROM system.parts
            WHERE database = {db:String} AND table = {tbl:String} AND active
            GROUP BY partition
            ORDER BY partition DESC
            LIMIT {lim:UInt32}
            """,
            parameters={"db": db, "tbl": table, "lim": max(1, min(limit, 1000))},
        )
        if qerr:
            return qerr
        return {
            "cluster": handle.name,
            "database": db,
            "table": table,
            "partitions": _rows_as_dicts(result),
            "count": result.row_count,
        }
