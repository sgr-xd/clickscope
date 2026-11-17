"""estimate_query_cost: shard scaling, the byte estimate and the per-server cap check.

The column sizes are a real 5.9-billion-row log table's (one server of two shards), with
the names made generic. The two queries below are shaped like ones that ran against it:
one died at the 10 GB byte cap after an estimate of 2.75M rows, the other read 17.7M rows
without trouble.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from clickscope.config import Limits
from clickscope.connection import QueryResult
from clickscope.tools import cost

TOTAL_ROWS = 5_918_306_328
SIZES = [
    ("tenant_id", "LowCardinality(String)", 5_981_837_135),
    ("source_type", "LowCardinality(String)", 5_982_613_081),
    ("timestamp", "DateTime64(3)", 47_344_907_312),
    ("metadata", "JSON", 3_355_260_756_265),
    ("srcip", "String", 107_894_365_199),
    ("appname", "String", 88_875_094_028),
    ("hostname", "String", 136_292_114_338),
    ("username", "String", 121_038_081_844),
    ("raw_log", "String", 11_390_079_285_792),
]
DIST = ("logs", "events", "Distributed('main', 'logs', 'events_local', rand())")

# Raw text read for every selected row: failed at the byte cap.
RAW_TEXT_SQL = """
SELECT hostname, username, toString(metadata.process) proc,
       any(JSONExtractString(raw_log, 'ProcessCommandLine')) cmd
FROM logs.events
PREWHERE tenant_id = 'acme' AND source_type = 'endpoint_process'
  AND timestamp >= '2026-01-01 00:00:00' AND timestamp < '2026-01-02 00:00:00'
WHERE positionCaseInsensitive(hostname, 'web05') > 0
GROUP BY hostname, username, proc
LIMIT 60
"""
# Cheap columns and JSON paths only: read 17.7M rows per server without trouble.
CHEAP_SQL = """
SELECT srcip, appname, toString(metadata.status) st, count() n
FROM logs.events
PREWHERE tenant_id = 'acme' AND source_type = 'vault'
  AND timestamp >= '2026-01-01 00:00:00' AND timestamp < '2026-01-10 00:00:00'
GROUP BY srcip, appname, st
LIMIT 60
"""


def _result(columns: list[str], rows: list[list[Any]]) -> QueryResult:
    return QueryResult(columns=columns, rows=rows, row_count=len(rows), elapsed_ms=0)


class FakeConnection:
    """Answers the system-table lookups and EXPLAIN ESTIMATE the estimate makes."""

    def __init__(self, rows_per_server: int, shards: int = 2, dist=(DIST,), estimate=True):
        self.rows_per_server = rows_per_server
        self.shards = shards
        self.dist = list(dist)
        self.estimate = estimate
        self.config = SimpleNamespace(limits=Limits())

    def execute(self, sql: str, parameters: dict[str, Any] | None = None) -> QueryResult:
        if sql.startswith("EXPLAIN ESTIMATE"):
            if not self.estimate:
                return _result(["database", "table", "parts", "rows", "marks"], [])
            return _result(
                ["database", "table", "parts", "rows", "marks"],
                [["logs", "events_local", 16, self.rows_per_server, 40]],
            )
        if "FROM system.tables" in sql and "engine = 'Distributed'" in sql:
            return _result(["database", "name", "engine_full"], [list(d) for d in self.dist])
        if "FROM system.tables" in sql:
            return _result(["total_rows"], [[TOTAL_ROWS]])
        if "FROM system.clusters" in sql:
            return _result(["n"], [[self.shards]])
        if "FROM system.columns" in sql:
            return _result(["name", "type", "data_uncompressed_bytes"], [list(s) for s in SIZES])
        raise AssertionError(f"unexpected query: {sql}")


class FakeMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self, **_: Any):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn

        return deco


class FakeAudit:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def record(self, **kwargs: Any) -> None:
        self.records.append(kwargs)


def _estimate(conn: FakeConnection, sql: str):
    decision = SimpleNamespace(allowed=True, sql=sql, query_hash="h", violations=[])
    handle = SimpleNamespace(
        name="test", connection=conn, policy=SimpleNamespace(evaluate=lambda *a, **k: decision)
    )
    registry = SimpleNamespace(get=lambda _c=None: handle, names=["test"])
    mcp, audit = FakeMCP(), FakeAudit()
    cost.register(mcp, registry, audit)
    return mcp.tools["estimate_query_cost"](sql), audit


class TestEstimate:
    def test_raw_text_query_is_flagged_before_it_fails(self) -> None:
        out, _ = _estimate(FakeConnection(2_748_363), RAW_TEXT_SQL)
        cap = out["cap_check"]
        assert cap["status"] == "near_cap"
        assert cap["suggested_windows"] == 2
        assert out["assessment"].startswith("Estimated at 54% of the per-server read cap")
        bytes_est = out["estimates"][0]["bytes_estimate"]
        assert bytes_est["heaviest_columns"][0]["column"] == "raw_log"
        assert bytes_est["not_counted"] == ["metadata"]

    def test_cheap_query_passes(self) -> None:
        out, _ = _estimate(FakeConnection(17_700_000), CHEAP_SQL)
        assert out["cap_check"]["status"] == "ok"
        assert "advice" not in out["cap_check"]

    def test_rows_are_scaled_to_every_shard(self) -> None:
        out, _ = _estimate(FakeConnection(1_000_000, shards=2), CHEAP_SQL)
        est = out["estimates"][0]
        assert est["rows"] == 1_000_000
        assert est["shards"] == 2
        assert est["rows_all_shards"] == 2_000_000
        assert out["total_rows_to_read"] == 2_000_000
        # The caps bind on each server, so they are checked against one shard.
        assert out["cap_check"]["rows_per_server"] == 1_000_000

    def test_a_tiny_share_is_selective_not_unknown(self) -> None:
        out, _ = _estimate(FakeConnection(56_819), CHEAP_SQL)
        assert out["estimates"][0]["fraction_of_table"] == 0.0
        assert out["verdict"] == "selective"
        assert "under 0.01%" in out["assessment"]

    def test_a_trivial_estimate_still_names_rows_and_is_audited(self) -> None:
        out, audit = _estimate(FakeConnection(0, estimate=False), "SELECT 1")
        assert out["verdict"] == "trivial"
        assert out["total_rows_to_read"] == 0
        assert audit.records and audit.records[0]["allowed"] is True


class TestShardCount:
    def test_query_on_the_distributed_table_is_scaled(self) -> None:
        assert cost._shard_count(FakeConnection(1), CHEAP_SQL, "logs", "events_local") == 2

    def test_query_on_the_local_table_is_not(self) -> None:
        sql = "SELECT count() FROM logs.events_local WHERE tenant_id = 'acme'"
        assert cost._shard_count(FakeConnection(1), sql, "logs", "events_local") == 1

    def test_a_name_inside_a_literal_does_not_count(self) -> None:
        sql = "SELECT count() FROM logs.other WHERE note = 'events'"
        assert cost._shard_count(FakeConnection(1), sql, "logs", "events_local") == 1

    def test_unquoted_names_and_current_database(self) -> None:
        dist = [("logs", "events", "Distributed(main, currentDatabase(), events_local)")]
        conn = FakeConnection(1, shards=3, dist=dist)
        assert cost._shard_count(conn, CHEAP_SQL, "logs", "events_local") == 3

    def test_no_distributed_table_means_one(self) -> None:
        conn = FakeConnection(1, dist=())
        assert cost._shard_count(conn, CHEAP_SQL, "logs", "events_local") == 1


class TestReferencedColumns:
    def test_json_path_is_not_charged_at_the_whole_column(self) -> None:
        full, by_path = cost._referenced_columns(CHEAP_SQL, SIZES)
        assert "metadata" not in full
        assert by_path == ["metadata"]
        assert {"srcip", "appname", "tenant_id", "timestamp"} <= set(full)
        assert "raw_log" not in full

    def test_bare_json_column_is_charged(self) -> None:
        full, _ = cost._referenced_columns("SELECT toString(metadata) FROM logs.events", SIZES)
        assert "metadata" in full

    def test_select_star_reads_everything(self) -> None:
        full, by_path = cost._referenced_columns("SELECT * FROM logs.events LIMIT 5", SIZES)
        assert set(full) == {name for name, _, _ in SIZES}
        assert by_path == []


class TestCapCheck:
    def test_over_the_byte_cap(self) -> None:
        cap = cost._cap_check(1_000, 12_000_000_000, Limits())
        assert cap["status"] == "over_cap"
        assert cap["suggested_windows"] == 3

    def test_unknown_bytes_checks_rows_only(self) -> None:
        cap = cost._cap_check(1_000, None, Limits())
        assert cap["status"] == "ok"
        assert "bytes_per_server" not in cap
        assert "only rows were checked" in cap["note"]
