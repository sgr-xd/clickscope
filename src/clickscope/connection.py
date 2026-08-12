"""ClickHouse connection handling.

One lazily-created client is reused for the process lifetime. clickhouse-connect
maintains its own HTTP pool, so a single client is the right unit.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from typing import Any

import clickhouse_connect
from clickhouse_connect.driver.client import Client

from .config import ClusterConfig, Limits


class QueryError(RuntimeError):
    """A query failed server-side. Message is sanitized before it reaches a caller."""


@dataclass(frozen=True)
class QueryResult:
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    elapsed_ms: int
    # ClickHouse reports what it actually touched; surfacing it lets a caller learn
    # the real cost of the query it just ran, not only the estimate.
    rows_read: int | None = None
    bytes_read: int | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "columns": self.columns,
            "rows": self.rows,
            "row_count": self.row_count,
            "elapsed_ms": self.elapsed_ms,
        }
        if self.rows_read is not None:
            out["rows_read"] = self.rows_read
        if self.bytes_read is not None:
            out["bytes_read"] = self.bytes_read
        return out


def read_only_settings(limits: Limits) -> dict[str, Any]:
    """Server-side caps for a single query.

    Ordering is load-bearing. ClickHouse applies these in the order it receives them,
    and `readonly=1` forbids *any* further setting change. So every limit is listed
    first and `readonly` is applied last — otherwise the server rejects the request
    with "Cannot modify ... setting in readonly mode".

    Applying `readonly` last also means it is in force by the time the SQL text itself
    is parsed, so a trailing `SETTINGS max_rows_to_read=...` inside the query cannot
    raise the ceiling we just set. The parser blocks that clause too; this is the
    backstop for anything the parser misses.
    """
    return {
        "max_execution_time": limits.max_execution_time,
        "max_result_rows": limits.max_result_rows,
        "max_rows_to_read": limits.max_rows_to_read,
        "max_bytes_to_read": limits.max_bytes_to_read,
        "max_memory_usage": limits.max_memory_usage,
        "max_threads": limits.max_threads,
        # Truncate at max_result_rows instead of raising, so a caller gets a usable
        # partial answer rather than an exception on a slightly-too-broad query.
        "result_overflow_mode": "break",
        "readonly": 1,
    }


class Connection:
    def __init__(self, config: ClusterConfig) -> None:
        self._config = config
        self._client: Client | None = None
        self._lock = threading.Lock()

    @property
    def config(self) -> ClusterConfig:
        return self._config

    def _get_client(self) -> Client:
        if self._client is None:
            with self._lock:
                if self._client is None:
                    s = self._config
                    try:
                        self._client = clickhouse_connect.get_client(
                            host=s.host,
                            port=s.port,
                            username=s.username,
                            password=s.password,
                            database=s.database,
                            secure=s.secure,
                            verify=s.verify,
                            connect_timeout=s.connect_timeout,
                            send_receive_timeout=s.send_receive_timeout,
                            client_name="clickscope",
                        )
                    except Exception as exc:  # noqa: BLE001 - surfaced as QueryError
                        raise QueryError(
                            f"cannot connect to {s.host}:{s.port} as {s.username!r}: "
                            f"{sanitize(str(exc), s)}"
                        ) from None
        return self._client

    def execute(
        self,
        sql: str,
        parameters: dict[str, Any] | None = None,
        extra_settings: dict[str, Any] | None = None,
    ) -> QueryResult:
        """Run SQL under read-only caps.

        Callers pass user-supplied SQL only after the policy engine has cleared it.
        Discovery tools use this with parameters and their own server-built SQL.
        """
        client = self._get_client()
        query_settings = read_only_settings(self._config.limits)
        if extra_settings:
            # Merge before `readonly` so the ordering guarantee above still holds.
            merged = {k: v for k, v in query_settings.items() if k != "readonly"}
            merged.update(extra_settings)
            merged["readonly"] = query_settings["readonly"]
            query_settings = merged

        try:
            result = client.query(sql, parameters=parameters or {}, settings=query_settings)
        except Exception as exc:  # noqa: BLE001 - normalized for the caller
            raise QueryError(sanitize(str(exc), self._config)) from None

        summary = getattr(result, "summary", None) or {}
        return QueryResult(
            columns=list(result.column_names),
            rows=[list(row) for row in result.result_rows],
            row_count=len(result.result_rows),
            elapsed_ms=_elapsed_ms(summary),
            rows_read=_as_int(summary.get("read_rows")),
            bytes_read=_as_int(summary.get("read_bytes")),
        )

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            finally:
                self._client = None


def _elapsed_ms(summary: dict[str, Any]) -> int:
    """Server-reported query time.

    clickhouse-connect exposes no duration attribute; the number comes back in the
    X-ClickHouse-Summary header as `elapsed_ns`.
    """
    nanos = _as_int(summary.get("elapsed_ns"))
    return round(nanos / 1_000_000) if nanos else 0


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


_PASSWORD_IN_URL = re.compile(r"(://[^:/@\s]+:)[^@/\s]+(@)")


def sanitize(message: str, config: ClusterConfig) -> str:
    """Strip credentials out of a server or driver error before it is returned.

    ClickHouse and the driver both echo connection details into error text; without
    this, a failed connection would hand the password straight back to the caller.
    """
    cleaned = _PASSWORD_IN_URL.sub(r"\1***\2", message)
    if config.password:
        cleaned = cleaned.replace(config.password, "***")
    return cleaned.strip()
