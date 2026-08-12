"""Shared helpers for tool modules."""

from __future__ import annotations

from typing import Any

from ..connection import QueryError, QueryResult
from ..policy import Decision
from ..registry import ClusterHandle, ClusterRegistry, UnknownCluster

# Every tool takes this argument. Kept in one place so the wording an agent reads is
# identical across all eleven of them.
CLUSTER_ARG_DOC = "Which configured cluster to use. Defaults to the default cluster."


def resolve(
    registry: ClusterRegistry, cluster: str | None
) -> tuple[ClusterHandle | None, dict[str, Any] | None]:
    """Look up a cluster, or produce an error payload naming the valid choices."""
    try:
        return registry.get(cluster), None
    except UnknownCluster as exc:
        return None, {
            "error": str(exc),
            "clusters": registry.names,
            "hint": "call list_clusters to see what is configured",
        }


def run(
    handle: ClusterHandle, sql: str, parameters: dict[str, Any] | None = None
) -> tuple[QueryResult | None, dict[str, Any] | None]:
    """Execute a ClickScope-authored query, turning failure into a payload.

    A tool that lets QueryError escape becomes a protocol-level error, which tells the
    caller nothing about which cluster failed. That matters most with several clusters
    configured, where one being unreachable must not look like a broken tool.
    """
    try:
        return handle.connection.execute(sql, parameters=parameters), None
    except QueryError as exc:
        return None, {"error": str(exc), "cluster": handle.name}


def blocked_response(decision: Decision, cluster: str) -> dict[str, Any]:
    """Shape a rejection so a model can act on it rather than just retrying."""
    return {
        "blocked": True,
        "cluster": cluster,
        "error": decision.rejection_message(),
        "violations": decision.violations,
        "hint": (
            "ClickScope is read-only. Rewrite as a single SELECT/WITH/SHOW/DESCRIBE/"
            "EXPLAIN statement over this cluster's own tables."
        ),
    }
