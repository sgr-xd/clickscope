"""ClickScope — a read-only ClickHouse MCP server."""

from .config import AppConfig, ClusterConfig, ConfigError, Limits
from .connection import Connection, QueryError, QueryResult
from .registry import ClusterHandle, ClusterRegistry, UnknownCluster

__all__ = [
    "AppConfig",
    "ClusterConfig",
    "ClusterHandle",
    "ClusterRegistry",
    "ConfigError",
    "Connection",
    "Limits",
    "QueryError",
    "QueryResult",
    "UnknownCluster",
]
