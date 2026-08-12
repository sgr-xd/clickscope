"""LIMIT handling — the only place ClickScope rewrites a query.

A missing LIMIT is the single most common way an agent accidentally asks for a whole
table, and `max_result_rows` alone gives a truncated result with no explanation. An
explicit LIMIT is visible in the SQL the caller gets back, so the truncation is legible.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .rules import mask_literals

# A trailing LIMIT, in any of ClickHouse's accepted spellings:
#   LIMIT n | LIMIT n, m | LIMIT n OFFSET m | LIMIT n BY expr
# Anchored to the end so a LIMIT inside a subquery is not mistaken for the outer one.
_TRAILING_LIMIT = re.compile(
    r"""
    \bLIMIT\s+
    (?P<first>\d+)
    (?:
        \s*,\s*(?P<second>\d+)          # LIMIT offset, count
      | \s+OFFSET\s+\d+                 # LIMIT count OFFSET offset
    )?
    \s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)

# LIMIT n BY expr is a different feature (top-n per group) and does not bound the total
# result, so a query using it still needs an outer LIMIT.
_LIMIT_BY = re.compile(r"\bLIMIT\s+\d+\s+BY\b", re.IGNORECASE)

# Statements that return a bounded result on their own.
_SELF_BOUNDED_HEADS = ("SHOW", "DESC", "DESCRIBE", "EXPLAIN")
_HEAD_RE = re.compile(r"\s*([A-Za-z_]+)")


@dataclass(frozen=True)
class LimitOutcome:
    sql: str
    effective_limit: int | None
    notes: list[str]


def apply(sql: str, default_limit: int, max_limit: int) -> LimitOutcome:
    """Ensure the statement carries a LIMIT no larger than `max_limit`."""
    stripped = sql.strip().rstrip(";").rstrip()
    masked = mask_literals(stripped)

    head_match = _HEAD_RE.match(masked)
    head = head_match.group(1).upper() if head_match else ""
    if head in _SELF_BOUNDED_HEADS:
        return LimitOutcome(sql=stripped, effective_limit=None, notes=[])

    match = _TRAILING_LIMIT.search(masked)
    if match is None:
        note = f"no LIMIT present; appended LIMIT {default_limit}"
        if _LIMIT_BY.search(masked):
            note = (
                f"LIMIT ... BY bounds rows per group, not the total; appended LIMIT {default_limit}"
            )
        return LimitOutcome(
            sql=f"{stripped} LIMIT {default_limit}",
            effective_limit=default_limit,
            notes=[note],
        )

    # With `LIMIT a, b` the row count is the second number; otherwise the first.
    count_group = "second" if match.group("second") is not None else "first"
    requested = int(match.group(count_group))
    if requested <= max_limit:
        return LimitOutcome(sql=stripped, effective_limit=requested, notes=[])

    # Clamp by rewriting just the count, leaving any offset intact.
    span_start, span_end = match.span(count_group)
    clamped = stripped[:span_start] + str(max_limit) + stripped[span_end:]
    return LimitOutcome(
        sql=clamped,
        effective_limit=max_limit,
        notes=[f"LIMIT {requested} exceeds the maximum of {max_limit}; clamped"],
    )
