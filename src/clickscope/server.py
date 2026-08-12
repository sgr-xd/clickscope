"""MCP application factory.

Tool modules each expose `register(mcp, registry, ...)` and are wired in here, so
adding a tool family never means editing the entrypoint.
"""

from __future__ import annotations

from typing import Any

from mcp.server import MCPServer

from .audit import AuditLog
from .config import AppConfig
from .registry import ClusterRegistry
from .tools import READ_ONLY

INSTRUCTIONS = """\
ClickScope gives read-only access to one or more ClickHouse clusters.

Work in this order, especially on tables you have not seen before:

1. `list_tables` / `describe_table` to learn the schema, primary key and partitioning.
2. `estimate_query_cost` to see how much a query would read BEFORE running it.
   If it reports a large fraction of the table, add a filter on the primary key or
   partition column and estimate again.
3. `run_select_query` to execute.

`validate_query` checks syntax without reading any data. Writes are not possible:
every query runs read-only under server-enforced row, byte, memory and time caps.

When more than one cluster is configured, pass `cluster` explicitly on every call.
Call `list_clusters` first to see what exists. There is no "current" cluster to
switch — the argument is the only thing that decides where a query goes, and every
response names the cluster that answered it.
"""


def create_server(config: AppConfig | None = None) -> tuple[MCPServer, ClusterRegistry]:
    config = config or AppConfig.from_env()
    registry = ClusterRegistry(config)
    audit = AuditLog(config.audit_path)

    mcp = MCPServer("clickscope", instructions=INSTRUCTIONS)

    @mcp.tool(annotations=READ_ONLY)
    def server_info() -> dict[str, Any]:
        """Report how ClickScope is configured and which limits are in force.

        Useful as a first call to confirm the target clusters and the caps that apply,
        without touching any data.
        """
        return {
            "clickscope_version": _version(),
            "default_cluster": registry.default_name,
            "clusters": registry.describe_all(),
            "audit_log_enabled": audit.enabled,
        }

    from .tools import cost, discovery, query

    discovery.register(mcp, registry)
    query.register(mcp, registry, audit)
    cost.register(mcp, registry, audit)

    return mcp, registry


def _version() -> str:
    try:
        from importlib.metadata import version

        return version("clickscope")
    except Exception:  # noqa: BLE001 - version is cosmetic
        return "unknown"
