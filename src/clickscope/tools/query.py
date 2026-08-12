"""Query execution. Everything here passes through the policy engine first."""

from __future__ import annotations

import re
from typing import Any

from mcp.server import MCPServer

from ..audit import AuditLog
from ..connection import QueryError
from ..registry import ClusterRegistry
from . import READ_ONLY
from ._common import blocked_response, resolve

# ClickHouse identifiers may contain more than this, but a name outside this set is
# rare enough that refusing it is a better trade than widening the escape surface.
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def quote_identifier(name: str, kind: str) -> str:
    """Validate then backtick-quote. Rejects anything that is not a plain identifier.

    Identifiers cannot be bound as parameters in every ClickHouse version, so this is
    the guarantee that a table name never carries SQL into the statement.
    """
    if not _IDENTIFIER.match(name or ""):
        raise ValueError(
            f"invalid {kind} name {name!r}: expected letters, digits and underscores, "
            "not starting with a digit"
        )
    return f"`{name}`"


def register(mcp: MCPServer, registry: ClusterRegistry, audit: AuditLog) -> None:
    @mcp.tool(annotations=READ_ONLY)
    def run_select_query(
        sql: str, limit: int | None = None, cluster: str | None = None
    ) -> dict[str, Any]:
        """Execute a read-only SELECT against ClickHouse.

        Run `estimate_query_cost` first on any query over an unfamiliar or large table —
        it reports what the query would read without reading it.

        The statement must be a single SELECT/WITH/SHOW/DESCRIBE/EXPLAIN. A missing
        LIMIT is added and an oversized one is clamped; the SQL actually executed comes
        back in `executed_sql`, and the cluster that answered in `cluster`.

        Args:
            sql: The statement to run.
            limit: Row limit for this call when the SQL has none of its own.
            cluster: Which configured cluster to use. Defaults to the default cluster.
        """
        handle, err = resolve(registry, cluster)
        if err:
            return err

        decision = handle.policy.evaluate(sql, default_limit=limit)
        if not decision.allowed:
            audit.record(
                tool="run_select_query",
                cluster=handle.name,
                sql=sql,
                query_hash=decision.query_hash,
                allowed=False,
                violations=decision.violations,
            )
            return blocked_response(decision, handle.name)

        try:
            result = handle.connection.execute(decision.sql)
        except QueryError as exc:
            audit.record(
                tool="run_select_query",
                cluster=handle.name,
                sql=decision.sql,
                query_hash=decision.query_hash,
                allowed=True,
                error=str(exc),
            )
            return {"error": str(exc), "cluster": handle.name, "executed_sql": decision.sql}

        audit.record(
            tool="run_select_query",
            cluster=handle.name,
            sql=decision.sql,
            query_hash=decision.query_hash,
            allowed=True,
            notes=decision.notes,
            effective_limit=decision.effective_limit,
            row_count=result.row_count,
            rows_read=result.rows_read,
            bytes_read=result.bytes_read,
            elapsed_ms=result.elapsed_ms,
        )

        payload = result.to_dict()
        payload["cluster"] = handle.name
        payload["executed_sql"] = decision.sql
        if decision.notes:
            payload["policy_notes"] = decision.notes
        if decision.effective_limit is not None and result.row_count >= decision.effective_limit:
            payload["truncated"] = True
            payload["policy_notes"] = payload.get("policy_notes", []) + [
                f"result reached the limit of {decision.effective_limit}; there may be more rows"
            ]
        return payload

    @mcp.tool(annotations=READ_ONLY)
    def sample_rows(
        table: str,
        database: str | None = None,
        limit: int = 10,
        cluster: str | None = None,
    ) -> dict[str, Any]:
        """Preview rows from a table without writing any SQL.

        Use this to see the shape of real values before composing a query. The
        statement is built here from validated identifiers, so no SQL is accepted.

        Args:
            table: Table name.
            database: Defaults to the cluster's database.
            limit: Rows to return (capped by the cluster's max limit).
            cluster: Which configured cluster to use. Defaults to the default cluster.
        """
        handle, err = resolve(registry, cluster)
        if err:
            return err

        db = database or handle.config.database
        try:
            qualified = f"{quote_identifier(db, 'database')}.{quote_identifier(table, 'table')}"
        except ValueError as exc:
            return {"error": str(exc), "cluster": handle.name}

        capped = max(1, min(limit, handle.config.limits.max_limit))
        sql = f"SELECT * FROM {qualified} LIMIT {capped}"

        try:
            result = handle.connection.execute(sql)
        except QueryError as exc:
            return {"error": str(exc), "cluster": handle.name, "executed_sql": sql}

        audit.record(
            tool="sample_rows",
            cluster=handle.name,
            sql=sql,
            query_hash="",
            allowed=True,
            row_count=result.row_count,
            rows_read=result.rows_read,
            elapsed_ms=result.elapsed_ms,
        )

        payload = result.to_dict()
        payload["cluster"] = handle.name
        payload["executed_sql"] = sql
        return payload
