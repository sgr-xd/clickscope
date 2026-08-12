"""Every statement here must be rejected. A failure in this file blocks release.

The list is append-only: when a bypass is found, it gets a line here before it gets a
fix in `rules.py`.
"""

from __future__ import annotations

import pytest

from clickscope.config import Limits
from clickscope.policy import PolicyEngine

ENGINE = PolicyEngine(Limits())

MUST_BLOCK = [
    # --- direct writes and DDL -------------------------------------------------
    "DROP TABLE users",
    "DROP DATABASE analytics",
    "INSERT INTO events VALUES (1)",
    "INSERT INTO events SELECT * FROM other",
    "ALTER TABLE events DELETE WHERE 1=1",
    "ALTER TABLE events UPDATE col = 1 WHERE 1=1",
    "DELETE FROM events WHERE 1=1",
    "TRUNCATE TABLE events",
    "CREATE TABLE x (a Int32) ENGINE = Memory",
    "RENAME TABLE a TO b",
    "ATTACH TABLE x",
    "DETACH TABLE x",
    "OPTIMIZE TABLE events FINAL",
    # --- access control and server state ---------------------------------------
    "GRANT SELECT ON *.* TO someone",
    "REVOKE SELECT ON *.* FROM someone",
    "KILL QUERY WHERE query_id = 'x'",
    "SYSTEM DROP MARK CACHE",
    "SET max_rows_to_read = 0",
    "USE other_database",
    # --- statement stacking ----------------------------------------------------
    "SELECT 1; DROP TABLE users",
    "SELECT 1 ; INSERT INTO t VALUES (1)",
    "SELECT /* comment */ 1; DROP TABLE t",
    # a comment that ends at the newline does not hide what follows it
    "SELECT 1 -- harmless\nDROP TABLE t",
    # --- reading outside the cluster -------------------------------------------
    "SELECT * FROM url('http://attacker.example/x', JSONEachRow)",
    "SELECT * FROM file('/etc/passwd', 'CSV')",
    "SELECT * FROM s3('http://attacker.example/b', 'CSV')",
    "SELECT * FROM remote('other:9000', 'system', 'users')",
    "SELECT * FROM mysql('h:3306', 'db', 't', 'u', 'p')",
    "SELECT * FROM executable('sh -c whoami', TabSeparated)",
    "SELECT * FROM hdfs('hdfs://x/y', 'CSV')",
    # --- exfiltration and cap evasion ------------------------------------------
    "SELECT * FROM events INTO OUTFILE '/tmp/dump.csv'",
    "SELECT 1 SETTINGS max_rows_to_read = 999999999999",
    "SELECT * FROM events LIMIT 10 SETTINGS readonly = 0",
    # --- credential and ACL tables ---------------------------------------------
    "SELECT * FROM system.users",
    "SELECT * FROM system.grants",
    "SELECT * FROM system.quotas",
    "SELECT * FROM system.row_policies",
    "SELECT name FROM system . users",
    # --- degenerate input ------------------------------------------------------
    "",
    "   ",
    "-- just a comment",
    "/* only a block comment */",
]


@pytest.mark.parametrize("sql", MUST_BLOCK, ids=lambda s: (s[:48] or "<empty>").replace("\n", " "))
def test_must_block(sql: str) -> None:
    decision = ENGINE.evaluate(sql)
    assert not decision.allowed, f"policy allowed a dangerous statement: {sql!r}"
    assert decision.violations, "a rejection must explain itself"
