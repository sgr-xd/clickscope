"""Column profiling.

Unlike discovery, this reads real table data — profiling a column means scanning it.
The server-side caps apply as they do everywhere, and the response reports `rows_read`
so the cost is visible rather than implied.

The SQL is built here from validated identifiers; no caller SQL is accepted.
"""

from __future__ import annotations

from typing import Any

from mcp.server import MCPServer

from ..connection import QueryError
from ..registry import ClusterRegistry
from . import READ_ONLY
from ._common import resolve
from .query import quote_identifier

# Types that cannot be ordered or grouped meaningfully. min/max and a top-values
# breakdown are skipped for these rather than failing the whole call.
_UNORDERABLE_PREFIXES = ("Array", "Map", "Tuple", "Nested", "AggregateFunction", "JSON")


def _is_orderable(column_type: str) -> bool:
    bare = column_type.replace("Nullable(", "").replace("LowCardinality(", "")
    return not bare.startswith(_UNORDERABLE_PREFIXES)


def register(mcp: MCPServer, registry: ClusterRegistry) -> None:
    @mcp.tool(annotations=READ_ONLY)
    def profile_column(
        table: str,
        column: str,
        database: str | None = None,
        top_n: int = 10,
        cluster: str | None = None,
    ) -> dict[str, Any]:
        """Profile one column: nulls, distinct values, range, and what is most common.

        The usual first question about an unfamiliar column — how much of it is empty,
        how many distinct values it holds, and what actually appears in it. Answers what
        a schema cannot: `describe_table` says a column is a String, this says it is a
        String with four distinct values, 12% of them null.

        This reads the column, so it costs something. On a large table run
        `estimate_query_cost` on `SELECT <column> FROM <table>` first, or narrow the
        table with a partition. The response reports `rows_read` either way.

        Args:
            table: Table name.
            column: Column to profile.
            database: Defaults to the cluster's database.
            top_n: How many of the most common values to return.
            cluster: Which configured cluster to use. Defaults to the default cluster.
        """
        handle, err = resolve(registry, cluster)
        if err:
            return err
        db = database or handle.config.database

        # Resolve the type first: it decides which aggregates are even valid, and it
        # confirms the column exists before anything scans the table.
        try:
            meta = handle.connection.execute(
                """
                SELECT type, is_in_primary_key, is_in_partition_key
                FROM system.columns
                WHERE database = {db:String} AND table = {tbl:String} AND name = {col:String}
                """,
                parameters={"db": db, "tbl": table, "col": column},
            )
        except QueryError as exc:
            return {"error": str(exc), "cluster": handle.name}

        if meta.row_count == 0:
            return {
                "cluster": handle.name,
                "error": f"column {column!r} not found on {db}.{table}",
                "hint": "call describe_table to see the available columns",
            }

        column_type = str(meta.rows[0][0])
        in_primary_key, in_partition_key = bool(meta.rows[0][1]), bool(meta.rows[0][2])

        try:
            qualified = f"{quote_identifier(db, 'database')}.{quote_identifier(table, 'table')}"
            col = quote_identifier(column, "column")
        except ValueError as exc:
            return {"error": str(exc), "cluster": handle.name}

        orderable = _is_orderable(column_type)
        # uniqExact is honest but expensive on a high-cardinality column; uniq is an
        # estimate within a couple of percent and costs far less. Say which it is.
        aggregates = [
            "count() AS rows_scanned",
            f"countIf({col} IS NULL) AS nulls",
            f"uniq({col}) AS distinct_approx",
        ]
        if orderable:
            aggregates += [f"min({col}) AS min_value", f"max({col}) AS max_value"]

        try:
            stats = handle.connection.execute(f"SELECT {', '.join(aggregates)} FROM {qualified}")
        except QueryError as exc:
            return {"error": str(exc), "cluster": handle.name, "column_type": column_type}

        row = dict(zip(stats.columns, stats.rows[0], strict=True)) if stats.rows else {}
        scanned = int(row.get("rows_scanned") or 0)
        nulls = int(row.get("nulls") or 0)

        payload: dict[str, Any] = {
            "cluster": handle.name,
            "database": db,
            "table": table,
            "column": column,
            "type": column_type,
            "is_in_primary_key": in_primary_key,
            "is_in_partition_key": in_partition_key,
            "rows_scanned": scanned,
            "nulls": nulls,
            "null_fraction": round(nulls / scanned, 4) if scanned else None,
            "distinct_approx": int(row.get("distinct_approx") or 0),
            "distinct_is_estimate": True,
            "rows_read": stats.rows_read,
            "elapsed_ms": stats.elapsed_ms,
        }
        if orderable:
            payload["min"] = row.get("min_value")
            payload["max"] = row.get("max_value")
        else:
            payload["note"] = (
                f"{column_type} cannot be ordered or grouped, so range and top values are omitted"
            )
            return payload

        try:
            top = handle.connection.execute(
                f"""
                SELECT {col} AS value, count() AS occurrences
                FROM {qualified}
                GROUP BY value
                ORDER BY occurrences DESC
                LIMIT {max(1, min(top_n, 100))}
                """
            )
        except QueryError as exc:
            payload["top_values_error"] = str(exc)
            return payload

        payload["top_values"] = [
            {
                "value": value,
                "occurrences": occurrences,
                "fraction": round(occurrences / scanned, 4) if scanned else None,
            }
            for value, occurrences in top.rows
        ]

        distinct = payload["distinct_approx"]
        if distinct and scanned:
            share = distinct / scanned
            if distinct <= 32:
                payload["assessment"] = (
                    f"only {distinct} distinct values — a good candidate for "
                    "LowCardinality, and a cheap column to filter or group by"
                )
            elif share > 0.9:
                payload["assessment"] = (
                    "almost every value is distinct — expensive to GROUP BY, and a poor "
                    "choice for a sorting key beyond the leading position"
                )
        return payload
