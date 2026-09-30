"""Half-open integer intervals and the interval algebra the kernel relies on.

Semantics, stated once and used everywhere in this package
    * An interval is ``[start, end)``. ``end`` is **not** included.
    * ``[p, p)`` is empty: it contains no instant and has measure ``0``. There
      is no closed single-point interval under half-open semantics; the interval
      that contains exactly one instant is ``[p, p + 1)`` (see
      :meth:`Interval.is_single_instant`). This is what makes union, difference
      and measure add up exactly, with no off-by-one bookkeeping.
    * Adjacency is **not** overlap: ``[0, 5)`` and ``[5, 9)`` do not overlap,
      they ``touch``.
    * ``end`` before ``start`` is a rejection, never a normalisation.

Ordering
    Every function returning more than one interval returns a ``tuple`` sorted
    by ``(start, end)`` ascending. :func:`merge` additionally returns the
    canonical form: pairwise non-overlapping, non-touching and non-empty.

This module supersedes the Phase 0 stub, which kept ``datetime`` endpoints and
had a latent defect: it reported an empty interval as overlapping anything that
spanned its anchor. ``overlaps`` keeps its name and its boolean role.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Final

from .errors import IntervalError
from .timebase import (
    Microsecond,
    as_microseconds,
    checked_add,
    datetimes_to_microseconds,
    from_microseconds,
)

if TYPE_CHECKING:  # pragma: no cover - annotation-only, keeps datetime out of runtime
    from datetime import datetime


class RangeRelation(Enum):
    """The thirteen Allen relations between two *non-empty* ranges.

    Returning the relation instead of a boolean is what lets a caller explain a
    rejection: "your reservation *starts* mine" and "mine is *during* yours"
    need different fixes but both answer ``overlaps() == True``.
    """

    BEFORE = "before"
    AFTER = "after"
    MEETS = "meets"
    MET_BY = "met_by"
    OVERLAPS = "overlaps"
    OVERLAPPED_BY = "overlapped_by"
    STARTS = "starts"
    STARTED_BY = "started_by"
    DURING = "during"
    CONTAINS = "contains"
    FINISHES = "finishes"
    FINISHED_BY = "finished_by"
    EQUALS = "equals"


INVERSE_RELATION: Final[dict[RangeRelation, RangeRelation]] = {
    RangeRelation.BEFORE: RangeRelation.AFTER,
    RangeRelation.AFTER: RangeRelation.BEFORE,
    RangeRelation.MEETS: RangeRelation.MET_BY,
    RangeRelation.MET_BY: RangeRelation.MEETS,
    RangeRelation.OVERLAPS: RangeRelation.OVERLAPPED_BY,
    RangeRelation.OVERLAPPED_BY: RangeRelation.OVERLAPS,
    RangeRelation.STARTS: RangeRelation.STARTED_BY,
    RangeRelation.STARTED_BY: RangeRelation.STARTS,
    RangeRelation.DURING: RangeRelation.CONTAINS,
    RangeRelation.CONTAINS: RangeRelation.DURING,
    RangeRelation.FINISHES: RangeRelation.FINISHED_BY,
    RangeRelation.FINISHED_BY: RangeRelation.FINISHES,
    RangeRelation.EQUALS: RangeRelation.EQUALS,
}


def _sign(value: int) -> int:
    """``-1``, ``0`` or ``1`` without floating point or a bool sneaking through."""
    return (value > 0) - (value < 0)


_OVERLAPPING_RELATION: Final[dict[tuple[int, int], RangeRelation]] = {
    (0, 0): RangeRelation.EQUALS,
    (0, -1): RangeRelation.STARTS,
    (0, 1): RangeRelation.STARTED_BY,
    (-1, 0): RangeRelation.FINISHED_BY,
    (1, 0): RangeRelation.FINISHES,
    (-1, -1): RangeRelation.OVERLAPS,
    (1, 1): RangeRelation.OVERLAPPED_BY,
    (-1, 1): RangeRelation.CONTAINS,
    (1, -1): RangeRelation.DURING,
}
"""Keyed by ``(sign(start_self - start_other), sign(end_self - end_other))``.

Only reachable once the operands are known to overlap, which is why all nine
cells are occupied: an empty cell here would be a KeyError in the middle of a
safety calculation.
"""


@dataclass(frozen=True, order=True)
class Interval:
    """Half-open ``[start, end)`` range of integer microseconds."""

    start: Microsecond
    end: Microsecond

    def __post_init__(self) -> None:
        lower = as_microseconds(self.start)
        upper = as_microseconds(self.end)
        if upper < lower:
            raise IntervalError(f"interval end {upper} precedes start {lower}")

    @classmethod
    def of(cls, start: int, end: int) -> Interval:
        """Ergonomic constructor from plain integers (validated on construction)."""
        return cls(as_microseconds(start), as_microseconds(end))

    @classmethod
    def from_datetimes(cls, start: datetime, end: datetime) -> Interval:
        """Boundary constructor: timezone-aware UTC ``datetime`` pair to kernel time."""
        lower, upper = datetimes_to_microseconds(start, end)
        return cls(lower, upper)

    @classmethod
    def empty_at(cls, at: int) -> Interval:
        """The empty interval anchored at an instant; the identity of ``union``."""
        point = as_microseconds(at)
        return cls(point, point)

    @property
    def measure(self) -> Microsecond:
        """Number of instants covered. Empty intervals measure ``0``."""
        return Microsecond(self.end - self.start)

    @property
    def is_empty(self) -> bool:
        return self.start == self.end

    @property
    def is_single_instant(self) -> bool:
        """True for ``[p, p + 1)``: exactly one microsecond instant covered."""
        return self.end - self.start == 1

    def as_datetimes(self) -> tuple[datetime, datetime]:
        """Render both endpoints as timezone-aware UTC datetimes (export only)."""
        return from_microseconds(self.start), from_microseconds(self.end)

    def shifted_by(self, delta: int) -> Interval:
        """Translate the whole range by ``delta`` microseconds.

        Raises:
            TimeDomainError: either endpoint would leave the int64 domain, so a
                clock jump cannot silently wrap a reservation into the past.
        """
        return Interval(
            checked_add(self.start, delta),
            checked_add(self.end, delta),
        )

    # Kept as ``shift`` for the name the routing engine spec uses.
    shift = shifted_by

    def contains_instant(self, at: int) -> bool:
        """True if ``at`` lies inside the half-open range."""
        instant = as_microseconds(at)
        return self.start <= instant < self.end

    def contains(self, other: Interval) -> bool:
        """Subset test. An empty ``other`` passes only when its anchor is inside.

        The empty set is a subset of everything, but for a airspace reservation
        an empty interval still carries a location in time, so a containment
        test that ignored it would hide a caller bug.
        """
        return self.start <= other.start and other.end <= self.end

    def overlaps(self, other: Interval) -> bool:
        """True when at least one instant is claimed by both ranges.

        Adjacency returns ``False``, and an empty operand always returns
        ``False`` even when the other interval spans its anchor.
        """
        if self.is_empty or other.is_empty:
            return False
        return self.start < other.end and other.start < self.end

    def touches(self, other: Interval) -> bool:
        """True when both are non-empty, do not overlap, and share one endpoint."""
        if self.is_empty or other.is_empty:
            return False
        return self.end == other.start or other.end == self.start

    def relation_to(self, other: Interval) -> RangeRelation:
        """The Allen relation of ``self`` to ``other``.

        The four disjoint cases are ordered comparisons; everything left overlaps,
        and an overlapping relation is fully determined by how the two starts and
        the two ends compare. That is a nine-entry table, so there is no chain of
        branches for a reviewer to misread.

        Raises:
            IntervalError: either operand is empty. Allen's algebra is defined
                over ranges that occupy at least one instant; inventing a
                relation for the empty set would give a wrong answer with
                confidence.
        """
        if self.is_empty or other.is_empty:
            raise IntervalError("Allen relations require non-empty intervals")
        if self.end < other.start:
            return RangeRelation.BEFORE
        if other.end < self.start:
            return RangeRelation.AFTER
        if self.end == other.start:
            return RangeRelation.MEETS
        if other.end == self.start:
            return RangeRelation.MET_BY
        return _OVERLAPPING_RELATION[(_sign(self.start - other.start), _sign(self.end - other.end))]

    def intersection(self, other: Interval) -> tuple[Interval, ...]:
        """``(overlap,)`` or ``()``; the result is already canonical."""
        lower = max(self.start, other.start)
        upper = min(self.end, other.end)
        if lower >= upper:
            return ()
        return (Interval(lower, upper),)

    def union(self, other: Interval) -> tuple[Interval, ...]:
        """One interval when they overlap or touch, two when they are disjoint.

        ``()`` only when both operands are empty.
        """
        return merge((self, other))

    def difference(self, other: Interval) -> tuple[Interval, ...]:
        """``self`` minus ``other``: zero, one or two pieces."""
        return subtract(self, (other,))

    def subtract(self, others: Iterable[Interval]) -> tuple[Interval, ...]:
        """``self`` minus every interval in ``others``, in ascending order."""
        return subtract(self, others)

    def clipped_to(self, window: Interval) -> tuple[Interval, ...]:
        """Intersection with ``window``, as a canonical one-or-zero tuple."""
        return self.intersection(window)

    def gap_to(self, other: Interval) -> Interval | None:
        """The free span between two non-overlapping ranges.

        ``None`` when they overlap or when they merely touch: a zero-length gap
        is reported as "no room", never as an empty interval a caller could
        mistake for a usable window.
        """
        if self.end <= other.start:
            lower, upper = self.end, other.start
        elif other.end <= self.start:
            lower, upper = other.end, self.start
        else:
            return None
        if lower >= upper:
            return None
        return Interval(as_microseconds(lower), as_microseconds(upper))

    def __str__(self) -> str:
        return f"[{self.start},{self.end})us"


def sort_key(interval: Interval) -> tuple[int, int]:
    """The documented total order for intervals: ``(start, end)`` ascending."""
    return (interval.start, interval.end)


def merge(intervals: Iterable[Interval]) -> tuple[Interval, ...]:
    """Canonical disjoint cover of ``intervals``, ascending.

    Touching ranges are merged as well as overlapping ones, because for a
    resource that is busy ``[0,5)`` and ``[5,9)`` there is no free instant at
    ``5`` and a canonical form must not depend on how many rows produced it.
    Empty intervals contribute nothing.

    Input order is irrelevant: the result is sorted before it is folded, so a
    set, a dict view or an unordered feed all produce byte-identical output.
    """
    ordered = sorted(intervals, key=sort_key)
    merged: list[Interval] = []
    for candidate in ordered:
        if candidate.is_empty:
            continue
        if merged and candidate.start <= merged[-1].end:
            previous = merged[-1]
            if candidate.end > previous.end:
                merged[-1] = Interval(previous.start, candidate.end)
            continue
        merged.append(candidate)
    return tuple(merged)


def intersect_all(intervals: Iterable[Interval]) -> Interval | None:
    """Common overlap of every interval, or ``None`` when there is none.

    An empty operand in the collection forces ``None``: it claims no instant,
    so it cannot be part of a shared commitment.
    """
    lower: Microsecond | None = None
    upper: Microsecond | None = None
    for interval in intervals:
        lower = interval.start if lower is None else max(lower, interval.start)
        upper = interval.end if upper is None else min(upper, interval.end)
    if lower is None or upper is None:
        return None
    if lower >= upper:
        return None
    return Interval(lower, upper)


def subtract(base: Interval, others: Iterable[Interval]) -> tuple[Interval, ...]:
    """``base`` with every part of ``others`` removed, ascending and canonical."""
    if base.is_empty:
        return ()
    cursor = base.start
    pieces: list[Interval] = []
    for other in merge(others):
        if other.end <= base.start:
            continue
        if other.start >= base.end:
            break
        if other.start > cursor:
            pieces.append(Interval(cursor, other.start))
        cursor = other.end
        if cursor >= base.end:
            return tuple(pieces)
    if cursor < base.end:
        pieces.append(Interval(cursor, base.end))
    return tuple(pieces)


def gaps_within(container: Interval, intervals: Iterable[Interval]) -> tuple[Interval, ...]:
    """Free spans of ``container`` not claimed by ``intervals``, ascending.

    Same computation as :func:`subtract`, named for the safe-interval reader:
    these are the candidate windows between commitments.
    """
    return subtract(container, intervals)


def total_measure(intervals: Iterable[Interval]) -> Microsecond:
    """Distinct instants covered, counting overlaps once.

    ``sum(iv.measure ...)`` double-counts; this merges first. The measure
    conservation property test depends on the distinction.
    """
    return Microsecond(sum((interval.measure for interval in merge(intervals)), 0))


def shift_all(intervals: Iterable[Interval], delta: int) -> tuple[Interval, ...]:
    """Translate every interval by ``delta``, preserving the ascending order."""
    return tuple(sorted((interval.shifted_by(delta) for interval in intervals), key=sort_key))


def overlaps_any(candidate: Interval, occupied: Iterable[Interval]) -> Interval | None:
    """The first occupied range overlapping ``candidate``, ascending.

    Returning the blocking interval rather than ``True`` is what makes a
    planner rejection auditable. ``None`` means no conflict.
    """
    for other in sorted(occupied, key=sort_key):
        if candidate.overlaps(other):
            return other
    return None
