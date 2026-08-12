"""Statement-shape rules.

Everything here is a *rejection* rule. Nothing in this module rewrites SQL — see
`limits.py` for the single exception (appending a LIMIT).

Keyword matching runs against a masked copy of the query in which comments and string
literals have been blanked out. Without that, `SELECT 'drop table users' AS note` would
be rejected, and `SELECT 1 --\\nDROP TABLE t` would be accepted. Both matter.
"""

from __future__ import annotations

import re

# Statements a read-only tool may legitimately issue. Because a query must begin with
# one of these AND may not contain a statement separator, no write statement can reach
# the server even if a write keyword survives the checks below.
_ALLOWED_HEADS = ("SELECT", "WITH", "SHOW", "DESC", "DESCRIBE", "EXPLAIN")

# Write/DDL words rejected anywhere in the query, as defence in depth behind the head
# check. Deliberately excluded because ClickHouse has ordinary functions or columns by
# these names, and rejecting them would break legitimate read queries:
#
#   truncate()  numeric function        replace()   string function
#   SET         appears as SETTINGS, handled separately, and as a column name
#   SYSTEM      every `system.*` table reference would match
#   USE / REPLACE / TRUNCATE are only valid as a statement head, which is already closed
_BLOCKED_KEYWORDS = (
    "INSERT",
    "UPDATE",
    "DELETE",
    "ALTER",
    "DROP",
    "CREATE",
    "ATTACH",
    "DETACH",
    "RENAME",
    "OPTIMIZE",
    "GRANT",
    "REVOKE",
    "KILL",
)

# Table functions that read something other than this cluster's own tables. `url` and
# `s3` make the ClickHouse server issue outbound requests (SSRF, and an exfiltration
# path); `file` reads the server's local disk; `executable` runs a program. A read-only
# server that permits these is not read-only in any sense an operator would recognise.
_BLOCKED_TABLE_FUNCTIONS = (
    "file",
    "url",
    "urlCluster",
    "s3",
    "s3Cluster",
    "remote",
    "remoteSecure",
    "cluster",
    "clusterAllReplicas",
    "hdfs",
    "hdfsCluster",
    "mysql",
    "postgresql",
    "mongodb",
    "jdbc",
    "odbc",
    "executable",
    "input",
    "azureBlobStorage",
    "deltaLake",
    "iceberg",
    "sqlite",
    "redis",
)

# System tables holding credentials, ACLs or cluster internals. The rest of `system.*`
# is genuinely useful for diagnosis and stays available.
_BLOCKED_SYSTEM_TABLES = (
    "users",
    "grants",
    "roles",
    "role_grants",
    "current_roles",
    "enabled_roles",
    "quotas",
    "quota_limits",
    "quota_usage",
    "settings_profiles",
    "settings_profile_elements",
    "user_directories",
    "row_policies",
    "zookeeper",
)

_STRING_OR_COMMENT = re.compile(
    r"""
      '(?:[^'\\]|\\.|'')*'      # single-quoted string, with backslash and '' escapes
    | "(?:[^"\\]|\\.|"")*"      # double-quoted identifier
    | `(?:[^`\\]|\\.|``)*`      # backtick identifier
    | --[^\n]*                  # line comment
    | \#[^\n]*                  # ClickHouse also accepts # line comments
    | /\*[\s\S]*?\*/            # block comment
    """,
    re.VERBOSE,
)

_BLOCKED_KEYWORD_RE = re.compile(r"\b(" + "|".join(_BLOCKED_KEYWORDS) + r")\b", re.IGNORECASE)
_BLOCKED_TABLE_FN_RE = re.compile(
    r"\b(" + "|".join(_BLOCKED_TABLE_FUNCTIONS) + r")\s*\(", re.IGNORECASE
)
_BLOCKED_SYSTEM_TABLE_RE = re.compile(
    r"\bsystem\s*\.\s*(" + "|".join(_BLOCKED_SYSTEM_TABLES) + r")\b", re.IGNORECASE
)
_INTO_OUTFILE_RE = re.compile(r"\bINTO\s+OUTFILE\b", re.IGNORECASE)
_SETTINGS_RE = re.compile(r"\bSETTINGS\b", re.IGNORECASE)
_HEAD_RE = re.compile(r"\s*([A-Za-z_]+)")


def mask_literals(sql: str) -> str:
    """Blank out comments and quoted spans, preserving length and line structure.

    Each masked span becomes spaces (newlines kept), so offsets in the masked copy still
    line up with the original if a rule ever needs to report a position.
    """

    def blank(match: re.Match[str]) -> str:
        return "".join("\n" if ch == "\n" else " " for ch in match.group(0))

    return _STRING_OR_COMMENT.sub(blank, sql)


def split_statements(masked: str) -> list[str]:
    """Split on semicolons that are real separators, not ones inside a literal."""
    return [part.strip() for part in masked.split(";") if part.strip()]


def check(sql: str) -> list[str]:
    """Return a list of violations. Empty means the statement shape is acceptable."""
    if not sql or not sql.strip():
        return ["query is empty"]

    masked = mask_literals(sql)
    if not masked.strip():
        return ["query contains no executable statement (comments only)"]

    violations: list[str] = []

    statements = split_statements(masked)
    if len(statements) > 1:
        violations.append(
            f"multi-statement queries are not allowed ({len(statements)} statements found)"
        )

    head_match = _HEAD_RE.match(masked)
    head = head_match.group(1).upper() if head_match else ""
    if head not in _ALLOWED_HEADS:
        violations.append(
            f"query must begin with one of {', '.join(_ALLOWED_HEADS)}; got {head or '?'}"
        )

    for keyword in sorted({m.group(1).upper() for m in _BLOCKED_KEYWORD_RE.finditer(masked)}):
        violations.append(f"forbidden keyword: {keyword}")

    for fn in sorted({m.group(1) for m in _BLOCKED_TABLE_FN_RE.finditer(masked)}):
        violations.append(f"forbidden table function: {fn}() — reads outside this cluster")

    for tbl in sorted({m.group(1).lower() for m in _BLOCKED_SYSTEM_TABLE_RE.finditer(masked)}):
        violations.append(f"forbidden system table: system.{tbl}")

    if _INTO_OUTFILE_RE.search(masked):
        violations.append("INTO OUTFILE is not allowed")

    if _SETTINGS_RE.search(masked):
        # An inline SETTINGS clause could otherwise raise max_rows_to_read past the
        # ceiling applied at the connection layer.
        violations.append("inline SETTINGS clause is not allowed")

    return violations
