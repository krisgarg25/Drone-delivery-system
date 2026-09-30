from __future__ import annotations

from datetime import datetime
from typing import Literal

Verdict = Literal["SAFE", "CONFLICT", "UNPROVEN"]

def to_microseconds(value: datetime) -> int | None:
    if value.tzinfo is None:
        return None
    return int(value.timestamp() * 1_000_000)

def check_interval_overlap(
    left: tuple[datetime, datetime],
    right: tuple[datetime, datetime],
) -> Verdict:
    """Deliberately simple, absolute-time checker.

    This is not the planner. It uses half-open intervals and returns CONFLICT when two
    reservations intersect. A production checker will additionally inspect 4D geometry,
    uncertainty, transition buffers and policy exceptions from committed records.
    """
    try:
        l_start, l_end = map(to_microseconds, left)
        r_start, r_end = map(to_microseconds, right)
    except (TypeError, ValueError, OverflowError):
        return "UNPROVEN"

    if None in (l_start, l_end, r_start, r_end):
        return "UNPROVEN"
    if l_start is None or l_end is None or r_start is None or r_end is None:
        return "UNPROVEN"
    if l_end < l_start or r_end < r_start:
        return "UNPROVEN"
    if l_start < r_end and r_start < l_end:
        return "CONFLICT"
    return "SAFE"
