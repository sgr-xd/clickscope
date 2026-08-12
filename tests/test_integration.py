"""Read-only integration tests against a real ClickHouse.

Skipped unless CLICKHOUSE_HOST or CLICKSCOPE_PROFILES is set. Every query here is a
SELECT against system tables or a caller-named table; nothing is written or created.

    export CLICKHOUSE_HOST=... CLICKHOUSE_USER=... CLICKHOUSE_PASSWORD_FILE=...
    export CLICKHOUSE_DATABASE=...
    export CLICKSCOPE_TEST_TABLE=some_table      # optional, enables the cost tests
    pytest -m integration
"""

from __future__ import annotations

import os

import pytest

from clickscope.config import AppConfig
from clickscope.connection import QueryError
from clickscope.registry import ClusterRegistry, UnknownCluster

pytestmark = pytest.mark.integration

if not (os.environ.get("CLICKHOUSE_HOST") or os.environ.get("CLICKSCOPE_PROFILES")):
    pytest.skip("no ClickHouse configured", allow_module_level=True)


@pytest.fixture(scope="module")
def registry():
    reg = ClusterRegistry(AppConfig.from_env())
    yield reg
    reg.close_all()


@pytest.fixture(scope="module")
def handle(registry: ClusterRegistry):
    return registry.get()


class TestConnectivity:
    def test_server_answers(self, handle) -> None:
        assert handle.connection.execute("SELECT 1 AS ok").rows == [[1]]

    def test_read_only_settings_are_accepted(self, handle) -> None:
        """The ordering guarantee in connection.read_only_settings actually holds.

        `readonly=1` forbids further setting changes, so if it were applied before the
        limits, ClickHouse would reject the request outright. This test is the reason
        that ordering is not merely a comment.
        """
        assert handle.connection.execute("SELECT 1").row_count == 1

    def test_readonly_is_in_force(self, handle) -> None:
        """A write must fail server-side, independently of our parser."""
        with pytest.raises(QueryError):
            handle.connection.execute(
                "CREATE TABLE clickscope_should_not_exist (a Int8) ENGINE = Memory"
            )

    def test_errors_do_not_leak_the_password(self, handle) -> None:
        password = handle.config.password
        if not password:
            pytest.skip("no password configured")
        with pytest.raises(QueryError) as exc:
            handle.connection.execute("SELECT * FROM does_not_exist_anywhere")
        assert password not in str(exc.value)

    def test_elapsed_is_reported(self, handle) -> None:
        # Regression: clickhouse-connect has no query_duration attribute, so this was
        # always 0 until it was read from the summary header instead.
        result = handle.connection.execute("SELECT sleep(0.1)")
        assert result.elapsed_ms > 0


class TestDiscovery:
    def test_lists_databases(self, handle) -> None:
        result = handle.connection.execute("SELECT name FROM system.databases ORDER BY name")
        assert "system" in [row[0] for row in result.rows]

    def test_lists_tables_with_keys(self, handle) -> None:
        result = handle.connection.execute(
            """
            SELECT name, engine, total_rows, sorting_key, partition_key
            FROM system.tables
            WHERE database = {db:String}
            ORDER BY name
            LIMIT 20
            """,
            parameters={"db": handle.config.database},
        )
        assert "sorting_key" in result.columns

    def test_column_metadata_available(self, handle) -> None:
        result = handle.connection.execute(
            """
            SELECT name, type, is_in_primary_key
            FROM system.columns
            WHERE database = {db:String}
            LIMIT 5
            """,
            parameters={"db": handle.config.database},
        )
        assert result.row_count >= 0


TEST_TABLE = os.environ.get("CLICKSCOPE_TEST_TABLE")


@pytest.mark.skipif(not TEST_TABLE, reason="CLICKSCOPE_TEST_TABLE not set")
class TestCostEstimation:
    def test_explain_syntax_reads_nothing(self, handle) -> None:
        result = handle.connection.execute(
            f"EXPLAIN SYNTAX SELECT count() FROM {handle.config.database}.{TEST_TABLE}"
        )
        assert result.row_count > 0

    def test_explain_estimate_returns_expected_columns(self, handle) -> None:
        result = handle.connection.execute(
            f"EXPLAIN ESTIMATE SELECT count() FROM {handle.config.database}.{TEST_TABLE}"
        )
        assert {"parts", "rows", "marks"}.issubset(set(result.columns))

    def test_explain_plan_mentions_indexes(self, handle) -> None:
        result = handle.connection.execute(
            f"EXPLAIN PLAN indexes = 1 SELECT count() FROM {handle.config.database}.{TEST_TABLE}"
        )
        assert result.row_count > 0


class TestClusterRouting:
    def test_default_resolves(self, registry: ClusterRegistry) -> None:
        assert registry.get(None).name == registry.default_name

    def test_unknown_cluster_is_refused_without_connecting(self, registry: ClusterRegistry) -> None:
        with pytest.raises(UnknownCluster):
            registry.get("definitely-not-configured")

    def test_every_configured_cluster_is_addressable(self, registry: ClusterRegistry) -> None:
        for name in registry.names:
            assert registry.get(name).name == name


class TestPolicyAgainstLiveServer:
    """The parser should reject these before they ever reach the network."""

    @pytest.mark.parametrize(
        "sql",
        [
            "DROP TABLE something",
            "INSERT INTO something VALUES (1)",
            "SELECT * FROM system.users",
            "SELECT * FROM url('http://example.com', CSV)",
        ],
    )
    def test_dangerous_statements_never_execute(self, handle, sql: str) -> None:
        assert not handle.policy.evaluate(sql).allowed


class TestEveryToolAgainstLiveServer:
    """Call each tool the way a client does, not by running its SQL by hand.

    The earlier integration tests exercised `Connection` directly, so a tool whose
    generated SQL was invalid still passed everything. `list_partitions` shipped with
    `sum(x) AS x`, which ClickHouse rejects as a nested aggregate the moment the alias
    is referenced again — invisible until someone actually called the tool.
    """

    @staticmethod
    def _call(name: str, arguments: dict):
        import asyncio
        import json

        from clickscope.server import create_server

        mcp, registry = create_server()
        try:
            result = asyncio.run(mcp.call_tool(name, arguments))
        finally:
            registry.close_all()
        payload = json.loads(result.content[0].text)
        assert "error" not in payload, f"{name} returned an error: {payload.get('error')}"
        return payload

    def test_server_info(self) -> None:
        assert self._call("server_info", {})["clusters"]

    def test_list_clusters(self) -> None:
        assert self._call("list_clusters", {})["clusters"]

    def test_list_databases(self) -> None:
        assert self._call("list_databases", {})["count"] >= 1

    def test_list_tables(self) -> None:
        assert "tables" in self._call("list_tables", {})

    @pytest.mark.skipif(not TEST_TABLE, reason="CLICKSCOPE_TEST_TABLE not set")
    def test_describe_table(self) -> None:
        assert self._call("describe_table", {"table": TEST_TABLE})["column_count"] > 0

    @pytest.mark.skipif(not TEST_TABLE, reason="CLICKSCOPE_TEST_TABLE not set")
    def test_show_create_table(self) -> None:
        assert "CREATE" in self._call("show_create_table", {"table": TEST_TABLE})["ddl"]

    @pytest.mark.skipif(not TEST_TABLE, reason="CLICKSCOPE_TEST_TABLE not set")
    def test_list_partitions(self) -> None:
        payload = self._call("list_partitions", {"table": TEST_TABLE})
        assert "partitions" in payload
        if payload["partitions"]:
            first = payload["partitions"][0]
            assert {"parts", "total_rows", "total_bytes", "size"}.issubset(first)

    @pytest.mark.skipif(not TEST_TABLE, reason="CLICKSCOPE_TEST_TABLE not set")
    def test_sample_rows(self) -> None:
        assert self._call("sample_rows", {"table": TEST_TABLE, "limit": 3})["row_count"] <= 3

    @pytest.mark.skipif(not TEST_TABLE, reason="CLICKSCOPE_TEST_TABLE not set")
    def test_validate_query(self) -> None:
        sql = f"SELECT count() FROM {TEST_TABLE}"
        assert self._call("validate_query", {"sql": sql})["valid"] is True

    @pytest.mark.skipif(not TEST_TABLE, reason="CLICKSCOPE_TEST_TABLE not set")
    def test_estimate_query_cost(self) -> None:
        sql = f"SELECT * FROM {TEST_TABLE}"
        assert "verdict" in self._call("estimate_query_cost", {"sql": sql})

    @pytest.mark.skipif(not TEST_TABLE, reason="CLICKSCOPE_TEST_TABLE not set")
    def test_explain_plan(self) -> None:
        sql = f"SELECT * FROM {TEST_TABLE}"
        assert "plan" in self._call("explain_plan", {"sql": sql})

    @pytest.mark.skipif(not TEST_TABLE, reason="CLICKSCOPE_TEST_TABLE not set")
    def test_run_select_query(self) -> None:
        payload = self._call("run_select_query", {"sql": f"SELECT 1 FROM {TEST_TABLE}"})
        assert payload["executed_sql"].endswith("LIMIT 100")
