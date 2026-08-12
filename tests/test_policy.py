"""Policy engine behaviour on legitimate queries.

The false-positive cases matter as much as the blocking ones: a guard that rejects
ordinary SQL gets switched off, and then it guards nothing.
"""

from __future__ import annotations

import pytest

from clickscope.config import Limits
from clickscope.policy import PolicyEngine
from clickscope.policy import rules as policy_rules

CAPS = Limits(default_limit=100, max_limit=1000)
ENGINE = PolicyEngine(CAPS)


MUST_ALLOW = [
    "SELECT 1",
    "SELECT count() FROM events WHERE ts > now() - INTERVAL 1 DAY",
    "WITH recent AS (SELECT * FROM events LIMIT 10) SELECT * FROM recent",
    "SHOW TABLES",
    "DESCRIBE TABLE events",
    "EXPLAIN SELECT 1",
    # `truncate` and `replace` are ordinary ClickHouse functions, not statements
    "SELECT truncate(3.7) AS t",
    "SELECT replace(name, 'a', 'b') FROM events",
    "SELECT replaceRegexpAll(msg, 'x', 'y') FROM events",
    # a write word inside a string literal is data, not a statement
    "SELECT 'drop table users' AS note",
    "SELECT * FROM events WHERE action = 'DELETE'",
    # ... and inside a comment
    "SELECT 1 -- drop table users",
    "SELECT /* insert into */ 1",
    # column names that merely start with a blocked word
    "SELECT create_table_query FROM system.tables",
    "SELECT granted_role_name FROM some_table",
    # non-sensitive system tables stay available for diagnosis
    "SELECT * FROM system.parts",
    "SELECT query_duration_ms FROM system.query_log",
    "SELECT * FROM system.tables WHERE database = 'default'",
]


@pytest.mark.parametrize("sql", MUST_ALLOW, ids=lambda s: s[:48].replace("\n", " "))
def test_must_allow(sql: str) -> None:
    decision = ENGINE.evaluate(sql)
    assert decision.allowed, f"policy rejected a legitimate query {sql!r}: {decision.violations}"


class TestLimitEnforcement:
    def test_appends_limit_when_absent(self) -> None:
        decision = ENGINE.evaluate("SELECT * FROM events")
        assert decision.sql == "SELECT * FROM events LIMIT 100"
        assert decision.effective_limit == 100
        assert decision.notes

    def test_leaves_acceptable_limit_alone(self) -> None:
        decision = ENGINE.evaluate("SELECT * FROM events LIMIT 5")
        assert decision.sql == "SELECT * FROM events LIMIT 5"
        assert decision.effective_limit == 5
        assert not decision.notes

    def test_clamps_oversized_limit(self) -> None:
        decision = ENGINE.evaluate("SELECT * FROM events LIMIT 500000")
        assert decision.sql == "SELECT * FROM events LIMIT 1000"
        assert decision.effective_limit == 1000
        assert "clamped" in " ".join(decision.notes)

    def test_offset_form_counts_the_second_number(self) -> None:
        # LIMIT offset, count -> the row count is the second value
        decision = ENGINE.evaluate("SELECT * FROM events LIMIT 20, 5000")
        assert decision.effective_limit == 1000
        assert decision.sql.endswith("LIMIT 20, 1000")

    def test_limit_offset_keyword_form_preserves_offset(self) -> None:
        decision = ENGINE.evaluate("SELECT * FROM events LIMIT 5000 OFFSET 20")
        assert decision.effective_limit == 1000
        assert decision.sql == "SELECT * FROM events LIMIT 1000 OFFSET 20"

    def test_subquery_limit_does_not_count_as_the_outer_limit(self) -> None:
        decision = ENGINE.evaluate("SELECT * FROM (SELECT * FROM events LIMIT 5) AS x")
        assert decision.sql.endswith("LIMIT 100")
        assert decision.effective_limit == 100

    def test_limit_by_still_needs_an_outer_limit(self) -> None:
        # LIMIT n BY expr bounds rows per group, not the total result
        decision = ENGINE.evaluate("SELECT * FROM events ORDER BY ts LIMIT 3 BY user")
        assert decision.sql.endswith("LIMIT 100")
        assert "per group" in " ".join(decision.notes)

    def test_self_bounded_statements_get_no_limit(self) -> None:
        for sql in ("SHOW TABLES", "DESCRIBE TABLE events", "EXPLAIN SELECT 1"):
            decision = ENGINE.evaluate(sql)
            assert decision.effective_limit is None
            assert "LIMIT" not in decision.sql.upper()

    def test_trailing_semicolon_is_stripped_before_appending(self) -> None:
        decision = ENGINE.evaluate("SELECT * FROM events;")
        assert decision.sql == "SELECT * FROM events LIMIT 100"

    def test_explain_wrapper_skips_limit_enforcement(self) -> None:
        decision = ENGINE.evaluate("SELECT * FROM events", enforce_limit=False)
        assert decision.allowed
        assert decision.sql == "SELECT * FROM events"
        assert decision.effective_limit is None


class TestLiteralMasking:
    def test_masking_preserves_length(self) -> None:
        sql = "SELECT 'abc' FROM t"
        assert len(policy_rules.mask_literals(sql)) == len(sql)

    def test_masking_preserves_newlines(self) -> None:
        sql = "SELECT 1 /* a\nb */ FROM t"
        assert policy_rules.mask_literals(sql).count("\n") == sql.count("\n")


class TestDecision:
    def test_rejection_message_lists_violations(self) -> None:
        decision = ENGINE.evaluate("DROP TABLE t")
        assert not decision.allowed
        message = decision.rejection_message()
        assert "rejected" in message.lower()
        assert "DROP" in message

    def test_query_hash_is_stable(self) -> None:
        a = ENGINE.evaluate("SELECT 1 LIMIT 1")
        b = ENGINE.evaluate("SELECT 1 LIMIT 1")
        assert a.query_hash == b.query_hash
        assert len(a.query_hash) == 16
