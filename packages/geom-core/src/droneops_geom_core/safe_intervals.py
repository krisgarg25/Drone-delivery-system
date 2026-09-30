"""Safe intervals: when may this aircraft enter that resource?

``technical-build/11`` section 4 rejects a time-expanded graph - multiplying
every corridor node by every time bucket blows up on a city graph - and asks a
smaller question instead: *when can this aircraft enter this edge without
intersecting existing commitments?* That question is answered exactly by
subtraction over padded windows, which is what this module does.

The rule, stated once
    An occupation ``[s, e)`` is protected on both sides by position/timing
    **uncertainty** ``u``, giving ``[s - u, e + u)``. A candidate that enters at
    ``t`` and traverses for ``d`` owns the resource over ``[t, t + d)`` and needs
    a **transition buffer** ``b`` on each side of its own occupancy, because it
    is still arriving or still clearing the resource while those instants pass.
    The candidate is admissible against that occupation if and only if

    .. code-block:: text

        t - b >= e + u      (entered after the previous occupation fully cleared)
        t + d + b <= s - u  (exit plus buffer clears before the next occupation)

    which makes the *entry-blocked* window of each occupation

    .. code-block:: text

        [s - u - d - b + 1,  e + u + b)

    The ``+ 1`` is not sloppiness: time is an integer count of microseconds, so
    the strict inequality ``t > x`` is exactly ``t >= x + 1``. Every endpoint is
    therefore a whole instant, which is what lets an answer be a half-open
    interval like everything else in this package.

    Substituting ``d = 0`` gives an entry-blocked window padded by ``u + b`` on
    each side of the occupation - the "padding applied to each side of every
    occupied window" the routing engine asks for - and ``d`` then slides only the
    left edge back by exactly the traversal time. That is the exit-entry-pair
    reasoning: a window is valid only when entry plus duration plus buffer clears
    the resource before the next occupation begins.

    A third conjunct is arithmetic rather than spatial: ``t + d + b`` must still
    fit in the int64 microsecond domain, because the ledger stores the release
    instant. An entry whose release cannot be written down is not offered, and
    :class:`EntryStatus` says so instead of overflowing on the way out.

Everything is a typed result. :func:`compute_safe_intervals` never returns a
boolean, never returns "no" without naming the occupation that blocked it, and
never returns an empty window list without a gap audit saying why.
"""

from __future__ import annotations

import bisect
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Final

from .errors import ResourceKeyError, SafeIntervalError
from .intervals import Interval, merge, sort_key
from .resources import ResourceKey
from .timebase import (
    INT64_MICROSECOND_MAX,
    Microsecond,
    as_duration_us,
    as_microseconds,
    saturating_add,
)

_DISCRETE_STEP: Final[int] = 1
"""Integer time turns a strict ``>`` into ``>= x + 1``; used in one place each."""

_MICROS_PER_SECOND: Final[int] = 1_000_000


class SafeIntervalStatus(Enum):
    """Outcome of a safe-interval query. Rejection is a first-class result."""

    FEASIBLE = "feasible"
    NO_WINDOW = "no_window"
    EMPTY_HORIZON = "empty_horizon"


class GapReason(Enum):
    """Why one examined gap did or did not produce a window."""

    FITS = "fits"
    GAP_TOO_SMALL = "gap_too_small"
    HORIZON_CLOSED = "horizon_closed"


class EntryStatus(Enum):
    """Verdict for a single proposed entry instant."""

    ADMISSIBLE = "admissible"
    BLOCKED_BY_OCCUPATION = "blocked_by_occupation"
    OUTSIDE_HORIZON = "outside_horizon"
    RELEASE_NOT_REPRESENTABLE = "release_not_representable"


@dataclass(frozen=True)
class SafeIntervalPadding:
    """Two separate margins, each applied to both sides of an occupation.

    They stay apart on purpose. ``uncertainty`` shrinks as a position source
    improves; ``transition_buffer`` is about getting in and out of a volume and
    does not. Collapsing them into one number makes a policy change look like a
    sensor improvement, and the policy version is what has to be auditable.
    """

    uncertainty_us: Microsecond
    transition_buffer_us: Microsecond

    def __post_init__(self) -> None:
        as_duration_us(self.uncertainty_us)
        as_duration_us(self.transition_buffer_us)

    @classmethod
    def from_seconds(cls, *, uncertainty_s: int, transition_buffer_s: int) -> SafeIntervalPadding:
        """Build from whole seconds, the unit the protected-volume YAML uses."""
        return cls(
            as_microseconds(uncertainty_s * _MICROS_PER_SECOND),
            as_microseconds(transition_buffer_s * _MICROS_PER_SECOND),
        )

    @property
    def expansion_us(self) -> Microsecond:
        """Padding applied to each side of an occupation for a zero-length pass."""
        return Microsecond(self.uncertainty_us + self.transition_buffer_us)


@dataclass(frozen=True)
class OccupiedWindow:
    """A commitment on a resource, carrying the identity that made it."""

    window: Interval
    label: str
    resource: ResourceKey | None = None

    def __post_init__(self) -> None:
        if self.window.is_empty:
            raise SafeIntervalError(
                f"occupied window {self.label!r} is empty and cannot block an entry"
            )

    @property
    def start(self) -> Microsecond:
        return self.window.start

    @property
    def end(self) -> Microsecond:
        return self.window.end

    def protected(self, padding: SafeIntervalPadding) -> Interval:
        """The occupation widened by uncertainty on each side."""
        return Interval(
            saturating_add(self.window.start, -padding.uncertainty_us),
            saturating_add(self.window.end, padding.uncertainty_us),
        )

    def entry_blocked_by(self, padding: SafeIntervalPadding, traversal_us: int) -> Interval:
        """The window of *entry* instants this occupation forbids."""
        duration = as_duration_us(traversal_us)
        back = padding.uncertainty_us + duration + padding.transition_buffer_us
        forward = padding.expansion_us
        return Interval(
            saturating_add(self.window.start, -back + _DISCRETE_STEP),
            saturating_add(self.window.end, forward),
        )


@dataclass(frozen=True)
class SafeIntervalQuery:
    """Every input to one safe-interval search, validated once.

    ``horizon`` is mandatory and finite. An implicit infinite horizon would make
    the answer a claim about all time, which no frozen world snapshot supports.

    The resource is the time-bounded one being entered. A corridor edge is
    reserved through its volume cells (``technical-build/01`` section 6 gives the
    bare ``corridor:<segment-id>`` no window), so an edge traversal asks this
    question per cell and takes the intersection of the answers.
    """

    resource: ResourceKey
    horizon: Interval
    traversal_us: Microsecond
    padding: SafeIntervalPadding
    occupied: tuple[OccupiedWindow, ...]

    def __post_init__(self) -> None:
        as_duration_us(self.traversal_us)
        if not self.resource.kind.takes_window:
            raise ResourceKeyError(
                f"{self.resource.kind.prefix} is not time-bounded; ask for safe intervals "
                "per volume_cell of the corridor edge and intersect the answers"
            )
        seen: set[str] = set()
        for window in self.occupied:
            if window.label in seen:
                raise SafeIntervalError(
                    f"duplicate occupied label {window.label!r}: two commitments claiming one "
                    "identity is a ledger defect, not a geometry question"
                )
            seen.add(window.label)

    @classmethod
    def build(
        cls,
        resource: ResourceKey,
        *,
        horizon: Interval,
        traversal_us: int,
        padding: SafeIntervalPadding,
        occupied: Sequence[tuple[Interval, str]],
    ) -> SafeIntervalQuery:
        """Convenience constructor from ``(window, label)`` pairs."""
        return cls(
            resource,
            horizon,
            as_duration_us(traversal_us),
            padding,
            tuple(OccupiedWindow(window, label, resource) for window, label in occupied),
        )

    def merged_occupied(self) -> tuple[OccupiedWindow, ...]:
        """Occupations ascending, non-overlapping and non-touching.

        Overlapping commitments on one resource collapse into a single blocked
        window: this function describes the airspace as it is claimed, it does
        not adjudicate whether that claim was legal. A merged window's label
        names every contributor, so a rejection can be traced back to the
        reservations that caused it however unordered the input feed was.
        """
        ordered = sorted(self.occupied, key=lambda item: (sort_key(item.window), item.label))
        if not ordered:
            return ()
        result: list[OccupiedWindow] = []
        for window in merge(item.window for item in ordered):
            contributors = sorted({item.label for item in ordered if window.contains(item.window)})
            result.append(
                OccupiedWindow(window, "+".join(contributors) or str(window), self.resource)
            )
        return tuple(result)


@dataclass(frozen=True)
class SafeWindow:
    """One admissible entry window, with the exits and release it implies."""

    entry: Interval
    exit: Interval
    release: Interval
    traversal_us: Microsecond
    bounded_before: OccupiedWindow | None
    bounded_after: OccupiedWindow | None

    @property
    def alternatives(self) -> Microsecond:
        """Count of admissible entry instants; never zero for a returned window."""
        return self.entry.measure

    @property
    def earliest_entry(self) -> Microsecond:
        return self.entry.start

    @property
    def latest_entry(self) -> Microsecond:
        """The last admissible entry instant, inclusive.

        ``entry.end`` is exclusive; reporting it as a latest entry would be one
        microsecond too late and would put an aircraft inside a padded
        commitment.
        """
        return Microsecond(self.entry.end - _DISCRETE_STEP)

    def occupancy_at(self, entry: int) -> Interval:
        """The protected occupancy ``[t, t + traversal)`` for a chosen entry."""
        instant = as_microseconds(entry)
        if not self.entry.contains_instant(instant):
            raise SafeIntervalError(
                f"entry {instant} is not admissible in window {self.entry}"
            )
        return Interval(instant, Microsecond(instant + self.traversal_us))


@dataclass(frozen=True)
class GapAudit:
    """A gap that was examined, and the reason it produced nothing.

    ``available_us`` is the room for a *zero-duration* pass: what is left of the
    gap after both uncertainties and both transition buffers have been paid. A
    traversal of ``d`` fits only when ``available_us >= d + 1``, because an entry
    instant has to exist and time is whole microseconds.
    """

    gap: Interval | None
    reason: GapReason
    available_us: Microsecond
    required_us: Microsecond
    bounded_before: OccupiedWindow | None
    bounded_after: OccupiedWindow | None


@dataclass(frozen=True)
class SafeIntervalResult:
    """The answer to a safe-interval query.

    ``windows`` is ascending by entry start and pairwise disjoint. ``blocked`` is
    the merged entry-blocked set clipped to the horizon, so an independent
    checker can re-derive the answer from ``query.occupied`` alone and compare
    notes - which is the entire point of keeping two implementations.
    """

    status: SafeIntervalStatus
    query: SafeIntervalQuery
    windows: tuple[SafeWindow, ...]
    blocked: tuple[Interval, ...]
    gap_audit: tuple[GapAudit, ...]

    @property
    def accepted_count(self) -> int:
        return len(self.windows)

    @property
    def rejected_count(self) -> int:
        return sum(1 for audit in self.gap_audit if audit.reason is not GapReason.FITS)

    def earliest(self) -> SafeWindow | None:
        """The window with the earliest admissible entry."""
        return self.windows[0] if self.windows else None

    def window_containing(self, entry: int) -> SafeWindow | None:
        """The window that admits ``entry``, or ``None``. Uses ascending order."""
        instant = as_microseconds(entry)
        for window in self.windows:
            if window.entry.contains_instant(instant):
                return window
        return None


@dataclass(frozen=True)
class EntryDecision:
    """Verdict on one proposed entry instant, with the binding constraint."""

    status: EntryStatus
    entry: Microsecond
    exit: Microsecond
    release: Microsecond
    blocking: OccupiedWindow | None
    earliest_next_entry: Microsecond | None


def compute_safe_intervals(query: SafeIntervalQuery) -> SafeIntervalResult:
    """Enumerate every admissible entry window inside ``query.horizon``.

    Order: ``windows`` ascending by entry start; ``gap_audit`` in the same order
    with exactly one entry per gap between consecutive merged occupations,
    including the two horizon edges. Every gap is examined once, so
    ``len(gap_audit) == len(windows) + rejected_count`` holds by construction.
    """
    padding = query.padding
    traversal = query.traversal_us
    horizon = query.horizon
    if horizon.is_empty:
        return SafeIntervalResult(
            SafeIntervalStatus.EMPTY_HORIZON, query, (), (), ()
        )
    occupied = query.merged_occupied()

    blocked = merge(item.entry_blocked_by(padding, traversal) for item in occupied)
    blocked_in_horizon = tuple(
        window for item in blocked for window in item.intersection(horizon)
    )

    windows: list[SafeWindow] = []
    audits: list[GapAudit] = []
    for before, after in _gap_bounds(occupied):
        bounds = _entry_bounds(horizon, before, after, padding=padding, traversal_us=traversal)
        if bounds.entry_high > bounds.entry_low:
            entry = Interval(as_microseconds(bounds.entry_low), as_microseconds(bounds.entry_high))
            exit_time = Interval(
                Microsecond(entry.start + traversal), Microsecond(entry.end + traversal)
            )
            release = Interval(
                Microsecond(exit_time.start + padding.transition_buffer_us),
                Microsecond(exit_time.end + padding.transition_buffer_us),
            )
            windows.append(SafeWindow(entry, exit_time, release, traversal, before, after))
            reason = GapReason.FITS
        else:
            reason = (
                GapReason.HORIZON_CLOSED
                if bounds.available_us <= 0
                else GapReason.GAP_TOO_SMALL
            )
        audits.append(
            GapAudit(
                bounds.gap,
                reason,
                bounds.available_us,
                as_duration_us(traversal),
                before,
                after,
            )
        )

    status = SafeIntervalStatus.FEASIBLE if windows else SafeIntervalStatus.NO_WINDOW
    return SafeIntervalResult(status, query, tuple(windows), blocked_in_horizon, tuple(audits))


@dataclass(frozen=True)
class _GapBounds:
    """Internal: the arithmetic of one gap, computed once per gap."""

    gap: Interval | None
    entry_low: int
    entry_high: int
    available_us: Microsecond


def _gap_bounds(
    occupied: tuple[OccupiedWindow, ...],
) -> tuple[tuple[OccupiedWindow | None, OccupiedWindow | None], ...]:
    """Consecutive occupation pairs, horizon edges first, in ascending order."""
    if not occupied:
        return ((None, None),)
    pairs: list[tuple[OccupiedWindow | None, OccupiedWindow | None]] = [(None, occupied[0])]
    for index in range(len(occupied) - 1):
        pairs.append((occupied[index], occupied[index + 1]))
    pairs.append((occupied[-1], None))
    return tuple(pairs)


def _entry_bounds(
    horizon: Interval,
    before: OccupiedWindow | None,
    after: OccupiedWindow | None,
    *,
    padding: SafeIntervalPadding,
    traversal_us: int,
) -> _GapBounds:
    """Entry bounds for one gap, with the zero-duration slack reported separately.

    Clipping to the horizon happens before the traversal is subtracted, never
    after: the horizon bounds *when an aircraft may enter*, not when it must
    arrive, so an entry near the horizon edge is legitimate even if its exit
    falls outside the window. Subtracting the traversal first and clipping after
    would reject those entries.

    The final ceiling is also the last entry whose *release* instant is still
    representable. A reservation whose exit cannot be stored in an int64 ledger
    column is not a reservation, so such an entry is not offered at all rather
    than returned and then overflowing on the derived exit.
    """
    expansion = padding.expansion_us
    low = horizon.start if before is None else saturating_add(before.end, expansion)
    ceiling = (
        horizon.end if after is None else saturating_add(after.start, -expansion + _DISCRETE_STEP)
    )
    gap_low = max(int(low), horizon.start)
    gap_high = min(int(ceiling), horizon.end)
    available = Microsecond(max(0, gap_high - gap_low))
    gap = (
        Interval(as_microseconds(gap_low), as_microseconds(gap_high))
        if gap_high > gap_low
        else None
    )
    entry_high = horizon.end if after is None else min(horizon.end, int(ceiling) - traversal_us)
    entry_high = min(entry_high, INT64_MICROSECOND_MAX - traversal_us - padding.transition_buffer_us)
    return _GapBounds(gap, gap_low, entry_high, available)


def classify_entry(query: SafeIntervalQuery, entry: int) -> EntryDecision:
    """Decide one entry instant against the same rule the windows are built from.

    Only the two neighbouring occupations are consulted: the occupied set is
    merged and disjoint, so a candidate can be violated only by the occupation
    that starts at or before its entry, or by the next one. Any earlier
    occupation that would violate the buffered occupancy implies that the
    immediate predecessor does too. This is the definitional oracle the property
    tests check the enumerated windows against.
    """
    instant = as_microseconds(entry)
    traversal = query.traversal_us
    padding = query.padding
    uncertainty = padding.uncertainty_us
    buffer_us = padding.transition_buffer_us
    exit_raw = instant + traversal
    release_raw = exit_raw + buffer_us
    exit_time = saturating_add(instant, traversal)
    release = saturating_add(exit_time, buffer_us)
    if not query.horizon.contains_instant(instant):
        return EntryDecision(EntryStatus.OUTSIDE_HORIZON, instant, exit_time, release, None, None)
    if release_raw > INT64_MICROSECOND_MAX:
        return EntryDecision(
            EntryStatus.RELEASE_NOT_REPRESENTABLE, instant, exit_time, release, None, None
        )

    occupied = query.merged_occupied()
    starts = [item.start for item in occupied]
    index = bisect.bisect_right(starts, instant)
    candidates: list[OccupiedWindow] = []
    if index > 0:
        candidates.append(occupied[index - 1])
    if index < len(occupied):
        candidates.append(occupied[index])
    for item in candidates:
        entered_too_early = instant - buffer_us < item.end + uncertainty
        intrudes_next = release > item.start - uncertainty
        if entered_too_early and intrudes_next:
            return EntryDecision(
                EntryStatus.BLOCKED_BY_OCCUPATION,
                instant,
                exit_time,
                release,
                item,
                saturating_add(item.end, padding.expansion_us),
            )
    return EntryDecision(EntryStatus.ADMISSIBLE, instant, exit_time, release, None, None)


def earliest_entry_on_or_after(query: SafeIntervalQuery, not_before: int) -> Microsecond | None:
    """Earliest admissible entry at or after ``not_before``, or ``None``.

    A planner asks this when a launch cannot happen before a crew handover or a
    charger window closes.
    """
    floor = as_microseconds(not_before)
    for window in compute_safe_intervals(query).windows:
        if window.entry.end > floor:
            return Microsecond(max(window.entry.start, floor))
    return None


def describe_result(result: SafeIntervalResult) -> tuple[str, ...]:
    """Deterministic one-line-per-window explanation for a decision record.

    Windows first, ascending by entry start, then one line per rejected gap in
    audit order. The caller never has to sort again.
    """
    lines: list[str] = [f"status={result.status.value}"]
    for window in result.windows:
        lines.append(
            f"enter [{window.entry.start},{window.entry.end})us "
            f"exit [{window.exit.start},{window.exit.end})us "
            f"traversal={window.traversal_us}us"
        )
    for audit in result.gap_audit:
        if audit.reason is GapReason.FITS:
            continue
        lines.append(
            f"rejected reason={audit.reason.value} available={audit.available_us}us "
            f"required={audit.required_us}us"
        )
    return tuple(lines)
