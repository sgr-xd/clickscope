"""Cluster registry.

Holds one lazily-created connection and policy engine per configured cluster. Lazy
matters: an unreachable staging box must not stop the server from starting and serving
production queries.

Cluster selection is an explicit per-call argument, never stored state. A `use_cluster`
tool would be tidier to call, but it leaves the active cluster invisible to the model
across a long conversation — and reading the wrong environment is exactly the mistake
this tool should make impossible.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from .config import AppConfig, ClusterConfig
from .connection import Connection
from .policy import PolicyEngine


class UnknownCluster(KeyError):
    """A caller named a cluster that is not configured."""

    def __init__(self, name: str, known: list[str]) -> None:
        self.name = name
        self.known = known
        super().__init__(name)

    def __str__(self) -> str:
        return f"unknown cluster {self.name!r}; configured clusters: {', '.join(self.known)}"


@dataclass(frozen=True)
class ClusterHandle:
    config: ClusterConfig
    connection: Connection
    policy: PolicyEngine

    @property
    def name(self) -> str:
        return self.config.name


class ClusterRegistry:
    def __init__(self, app: AppConfig) -> None:
        self._app = app
        self._handles: dict[str, ClusterHandle] = {}
        self._lock = threading.Lock()

    @property
    def names(self) -> list[str]:
        return sorted(self._app.clusters)

    @property
    def default_name(self) -> str:
        return self._app.default_cluster

    def config(self, name: str) -> ClusterConfig:
        try:
            return self._app.clusters[name]
        except KeyError:
            raise UnknownCluster(name, self.names) from None

    def get(self, name: str | None = None) -> ClusterHandle:
        """Resolve a cluster name to its handle, creating the connection on first use."""
        resolved = (name or self._app.default_cluster).strip()
        config = self.config(resolved)

        handle = self._handles.get(resolved)
        if handle is not None:
            return handle

        with self._lock:
            handle = self._handles.get(resolved)
            if handle is None:
                handle = ClusterHandle(
                    config=config,
                    connection=Connection(config),
                    policy=PolicyEngine(config.limits),
                )
                self._handles[resolved] = handle
        return handle

    def describe_all(self) -> list[dict[str, object]]:
        out = []
        for name in self.names:
            entry = self._app.clusters[name].describe()
            entry["is_default"] = name == self._app.default_cluster
            entry["connected"] = name in self._handles
            out.append(entry)
        return out

    def close_all(self) -> None:
        for handle in self._handles.values():
            handle.connection.close()
        self._handles.clear()
