"""Validation and cost estimation.

These are the tools that make a read-only ClickHouse server genuinely safe to point an
agent at: all three ask the planner what a query *would* do, and none of them read a
single row of table data.
"""

from __future__ import annotations

import re
from typing import Any

from mcp.server import MCPServer

from ..audit import AuditLog
from ..connection import Connection, QueryError
from ..registry import ClusterRegistry
from . import READ_ONLY
from ._common import blocked_response, resolve

# Above this share of a table, a query is doing a scan rather than a lookup.
_FULL_SCAN_FRACTION = 0.5
_HEAVY_FRACTION = 0.1


def register(mcp: MCPServer, registry: ClusterRegistry, audit: AuditLog) -> None:
    def _prepare(sql: str, tool: str, cluster: str | None):
        """Resolve the cluster and clear the statement, or return an error payload."""
        handle, err = resolve(registry, cluster)
        if err:
            return None, None, err

        decision = handle.policy.evaluate(sql, enforce_limit=False)
        if not decision.allowed:
            audit.record(
                tool=tool,
                cluster=handle.name,
                sql=sql,
                query_hash=decision.query_hash,
                allowed=False,
                violations=decision.violations,
            )
            return None, None, blocked_response(decision, handle.name)
        return handle, decision, None

    @mcp.tool(annotations=READ_ONLY)
    def validate_query(sql: str, cluster: str | None = None) -> dict[str, Any]:
        """Check whether a query is valid SQL, without running it.

        Uses EXPLAIN SYNTAX, so nothing is read and nothing is computed — this is the
        cheap way to check a query you just composed, instead of running it with
        LIMIT 1 and hoping.

        Args:
            sql: The statement to check.
            cluster: Which configured cluster to use. Defaults to the default cluster.
        """
        handle, decision, err = _prepare(sql, "validate_query", cluster)
        if err:
            return err

        # EXPLAIN SYNTAX parses but does not resolve names: it happily accepts a
        # column that does not exist, so on its own it would call an unrunnable query
        # valid. EXPLAIN PLAN builds the query plan, which requires every name to
        # resolve, and still reads no data.
        try:
            handle.connection.execute(f"EXPLAIN PLAN {decision.sql}")
        except QueryError as exc:
            return {"valid": False, "cluster": handle.name, "error": str(exc)}

        normalized = None
        try:
            syntax = handle.connection.execute(f"EXPLAIN SYNTAX {decision.sql}")
            normalized = "\n".join(str(row[0]) for row in syntax.rows)
        except QueryError:
            # The query is valid; only the pretty-printed form is unavailable.
            pass

        payload = {
            "valid": True,
            "cluster": handle.name,
            "note": "syntax, names and plan all resolved; no data was read",
        }
        if normalized:
            payload["normalized_sql"] = normalized
        return payload

    @mcp.tool(annotations=READ_ONLY)
    def estimate_query_cost(sql: str, cluster: str | None = None) -> dict[str, Any]:
        """Estimate what a query would read, before running it.

        Uses EXPLAIN ESTIMATE to report the parts, rows and marks ClickHouse would
        touch per table, then compares that against each table's total row count.

        Call this before `run_select_query` on any large or unfamiliar table. If the
        verdict is `full_scan`, add a filter on the table's partition key or the
        leading column of its sorting key (see `list_tables`) and estimate again.

        Args:
            sql: The statement to estimate.
            cluster: Which configured cluster to use. Defaults to the default cluster.
        """
        handle, decision, err = _prepare(sql, "estimate_query_cost", cluster)
        if err:
            return err

        try:
            result = handle.connection.execute(f"EXPLAIN ESTIMATE {decision.sql}")
        except QueryError as exc:
            return {
                "error": str(exc),
                "cluster": handle.name,
                "hint": "run validate_query first to check the syntax",
            }

        estimates = [dict(zip(result.columns, row, strict=True)) for row in result.rows]
        if not estimates:
            return {
                "cluster": handle.name,
                "estimates": [],
                "verdict": "trivial",
                "assessment": "the planner expects to read no table data",
            }

        total_rows_to_read = 0
        for est in estimates:
            rows_to_read = _int(est.get("rows"))
            total_rows_to_read += rows_to_read or 0
            table_total = _table_total_rows(
                handle.connection, est.get("database"), est.get("table")
            )
            est["table_total_rows"] = table_total
            if table_total and rows_to_read is not None and table_total > 0:
                est["fraction_of_table"] = round(rows_to_read / table_total, 4)

        worst = max(
            (e.get("fraction_of_table") for e in estimates if e.get("fraction_of_table")),
            default=None,
        )
        verdict, assessment = _assess(worst, total_rows_to_read)

        audit.record(
            tool="estimate_query_cost",
            cluster=handle.name,
            sql=decision.sql,
            query_hash=decision.query_hash,
            allowed=True,
            rows_read=total_rows_to_read,
        )

        return {
            "cluster": handle.name,
            "estimates": estimates,
            "total_rows_to_read": total_rows_to_read,
            "verdict": verdict,
            "assessment": assessment,
            "note": "estimate only; no table data was read",
        }

    @mcp.tool(annotations=READ_ONLY)
    def explain_plan(sql: str, cluster: str | None = None) -> dict[str, Any]:
        """Show the query plan, including how much the indexes actually pruned.

        Uses EXPLAIN PLAN with indexes enabled. When `estimate_query_cost` says a query
        is expensive, this shows why: `granules_selected` against `granules_total` is
        the real measure of whether the primary key did any work.

        Args:
            sql: The statement to explain.
            cluster: Which configured cluster to use. Defaults to the default cluster.
        """
        handle, decision, err = _prepare(sql, "explain_plan", cluster)
        if err:
            return err

        try:
            result = handle.connection.execute(f"EXPLAIN PLAN indexes = 1 {decision.sql}")
        except QueryError as exc:
            return {"error": str(exc), "cluster": handle.name}

        lines = [str(row[0]) for row in result.rows]
        payload: dict[str, Any] = {
            "cluster": handle.name,
            "plan": "\n".join(lines),
            "note": "plan only; no table data was read",
        }
        payload.update(_parse_pruning(lines))
        return payload


_GRANULES = re.compile(r"Granules:\s*(\d+)\s*/\s*(\d+)")
_PARTS = re.compile(r"Parts:\s*(\d+)\s*/\s*(\d+)")


def _parse_pruning(lines: list[str]) -> dict[str, Any]:
    """Measure how much the indexes actually pruned.

    ClickHouse lists each index stage with `Parts: kept/seen` and
    `Granules: kept/seen`, each stage narrowing what the next one sees. So the total
    is the *first* stage's denominator and the survivors are the *last* stage's
    numerator — comparing within a single stage understates the pruning.

    The presence of an `Indexes:` section says nothing on its own: a filter that misses
    the key still produces one, reporting 109/110 granules kept. Only the ratio tells
    you whether the index did any work.
    """
    granule_pairs: list[tuple[int, int]] = []
    part_pairs: list[tuple[int, int]] = []
    stages: list[dict[str, Any]] = []
    # Stage names (MinMax, Partition, PrimaryKey) sit one level in from `Indexes:`.
    # Anchoring to that indent keeps the column names listed under `Keys:` — which are
    # also bare words on their own line — from being mistaken for stages.
    stage_indent: int | None = None

    for line in lines:
        stripped = line.strip()
        if stripped == "Indexes:":
            stage_indent = len(line) - len(line.lstrip()) + 2
            continue

        granules = _GRANULES.search(line)
        parts = _PARTS.search(line)
        indent = len(line) - len(line.lstrip())

        if stage_indent is not None and indent == stage_indent and stripped and ":" not in stripped:
            stages.append({"index": stripped})
        if parts:
            part_pairs.append((int(parts.group(1)), int(parts.group(2))))
            if stages:
                stages[-1]["parts"] = f"{parts.group(1)}/{parts.group(2)}"
        if granules:
            granule_pairs.append((int(granules.group(1)), int(granules.group(2))))
            if stages:
                stages[-1]["granules"] = f"{granules.group(1)}/{granules.group(2)}"

    if not granule_pairs:
        return {
            "uses_index": False,
            "assessment": "no index section in the plan — this reads the table in full",
        }

    total = granule_pairs[0][1]
    selected = granule_pairs[-1][0]
    fraction = selected / total if total else None
    # The index only counts as working if it removed a real share of the granules.
    # Keeping 97% of them still produces an `Indexes:` section and is still a scan.
    effective = fraction is not None and fraction < _FULL_SCAN_FRACTION

    out: dict[str, Any] = {
        "uses_index": effective,
        "granules_selected": selected,
        "granules_total": total,
        "index_stages": [s for s in stages if "granules" in s or "parts" in s],
    }
    if part_pairs:
        out["parts_selected"] = part_pairs[-1][0]
        out["parts_total"] = part_pairs[0][1]
    if fraction is not None:
        out["granule_fraction"] = round(fraction, 4)
        if effective:
            out["assessment"] = (
                f"indexes narrowed the read to {selected} of {total} granules "
                f"({fraction * 100:.1f}%)"
            )
        else:
            out["assessment"] = (
                f"indexes kept {selected} of {total} granules ({fraction * 100:.1f}%) — "
                "effectively a full scan. Filter on the partition key or the leading "
                "sorting-key column instead."
            )
    return out


def _int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _table_total_rows(conn: Connection, database: Any, table: Any) -> int | None:
    """Total rows in a table, or None if it cannot be determined.

    A missing count is not an error: the table may be a view, or the user may not be
    able to see it in system.tables. The estimate is still useful without it.
    """
    if not database or not table:
        return None
    try:
        result = conn.execute(
            """
            SELECT total_rows
            FROM system.tables
            WHERE database = {db:String} AND name = {tbl:String}
            """,
            parameters={"db": str(database), "tbl": str(table)},
        )
    except QueryError:
        return None
    if result.row_count == 0:
        return None
    return _int(result.rows[0][0])


def _assess(worst_fraction: float | None, total_rows: int) -> tuple[str, str]:
    if worst_fraction is None:
        return (
            "unknown",
            f"the planner expects to read about {total_rows:,} rows; the table's total "
            "row count is unavailable, so the share of the table cannot be computed",
        )
    pct = worst_fraction * 100
    if worst_fraction >= _FULL_SCAN_FRACTION:
        return (
            "full_scan",
            f"this reads roughly {pct:.1f}% of the table ({total_rows:,} rows). Add a "
            "filter on the partition key or the leading sorting-key column, then "
            "estimate again.",
        )
    if worst_fraction >= _HEAVY_FRACTION:
        return (
            "heavy",
            f"this reads roughly {pct:.1f}% of the table ({total_rows:,} rows). "
            "Workable, but a tighter time or key filter would cut it considerably.",
        )
    return (
        "selective",
        f"this reads roughly {pct:.2f}% of the table ({total_rows:,} rows) — the index "
        "is doing its job.",
    )
