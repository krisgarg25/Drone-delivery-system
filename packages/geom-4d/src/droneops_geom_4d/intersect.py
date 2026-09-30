"""The decision procedure: do two swept protected volumes ever occupy one place?

Why this is a *sweep* and not a stack of frames
    ``01-technical-architecture.md`` section 8 requires conflict checking to use
    swept or conservatively discretised segments "so fast aircraft cannot jump
    through each other between frames". Two aircraft 40 m apart closing at 40 m/s
    meet in half a second. A 1 Hz sampler sees clearance at frame *n* and
    clearance at frame *n+1* and calls that safe. This routine is the package's
    reason to exist: it asks about **every** instant of the overlap, not a sample.

How it stays exact
    A swept prism moves linearly in time between breakpoints, with integer
    millimetre endpoints at integer microsecond instants. Relative position is
    therefore linear, and horizontal contact ``|p(t)|^2 <= R^2`` is a quadratic in
    ``t`` with rational coefficients. Instead of taking square roots (floats, and a
    tolerance nobody can audit), the kernel asks a question that needs none:
    *does this quadratic reach a non-positive value on the integer instants of the
    window?* A convex quadratic on an integer interval attains its minimum at an
    endpoint or at one of the two integers bracketing the vertex, and its sublevel
    set is one contiguous run of instants, so the run's bounds come from binary
    search on a monotone predicate. Everything is ``Fraction`` and integer
    arithmetic: same inputs, same verdict, on every replay, no epsilon anywhere.

Padded time is stationary time
    Every prism is widened at both ends by the policy's timing uncertainty (and,
    on a manoeuvring leg, its transition buffer). Inside a pad the body does not
    move: its position is clamped to the leg end. The solver therefore derives each
    sub-window's velocity from the two instants that bound it rather than from the
    leg's own slope, which would have extrapolated the aircraft backwards through
    the pre-pad and forwards through the post-pad - and would have reported
    contacts that never happened.

Time semantics
    Kernel intervals are half-open, ``[start, end)``. Geometric contact is
    evaluated over the **closed** set of integer instants a volume is alive on,
    because the instant a leg ends is an instant the body is somewhere. That is
    carried honestly as the half-open interval ``[first_instant, last_instant + 1)``,
    so ledger occupancy and geometric contact share one type with one meaning and
    neither quietly absorbs the other's boundary.

Uncertainty is a verdict, not an exception
    Two prisms whose altitudes live in different reference frames, or whose
    millimetres come from different projected planes, cannot be compared without
    inventing a datum. The answer is ``UNPROVEN``, never ``SAFE`` (conventions
    section 2: missing information means uncertainty, never clear airspace).
    Invalid footprint geometry is likewise ``UNPROVEN``.

What a deliberately different implementation should do instead
    This is the fast geometry path: swept prisms indexed onto ``geom_core`` cells
    and absolute-time windows, a closed-form prism-pair test for the pairs that
    survive the pre-filter, and margins read from the versioned policy file. An
    independent checker (``conflict_checker``, invariant I1) must not import this
    package and should reach its answer by a visibly different route: continuous
    absolute-time sweep on shapely geometry with a time carried on each vertex,
    conservative temporal overlap over whole absolute intervals, protected volumes
    recomputed from raw plan points rather than from a policy object, and no cell
    or bucket pre-filter at all. Two implementations that agree are evidence; one
    implementation is an assertion. Where they disagree is a release blocker.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from fractions import Fraction
from typing import Protocol

from droneops_geom_core import (
    AltitudeBand,
    AltitudeReference,
    Interval,
    Microsecond,
    Millimetre,
    as_millimetres,
)

from . import shapes


class Verdict(StrEnum):
    """The three-way answer. ``UNPROVEN`` blocks release; it is not a warning."""

    SAFE = "SAFE"
    CONFLICT = "CONFLICT"
    UNPROVEN = "UNPROVEN"


class VerdictCause(StrEnum):
    """Why the verdict is what it is, so a trace can explain itself."""

    TIME_DISJOINT = "TIME_DISJOINT"
    VERTICAL_CLEAR = "VERTICAL_CLEAR"
    HORIZONTAL_CLEAR = "HORIZONTAL_CLEAR"
    NEVER_SIMULTANEOUS = "NEVER_SIMULTANEOUS"
    CONTACT = "CONTACT"
    REFERENCE_MISMATCH = "REFERENCE_MISMATCH"
    INVALID_GEOMETRY = "INVALID_GEOMETRY"
    EMPTY_INPUT = "EMPTY_INPUT"


class SweepBasis(StrEnum):
    """How a volume was generated, and therefore what its verdict is worth."""

    CONTINUOUS = "CONTINUOUS"
    DISCRETISED = "DISCRETISED"
    FRAME_SAMPLE = "FRAME_SAMPLE"


#: How close each SAFE cause is to a conflict, used to pick the weakest evidence
#: when a profile pair is cleared by many prism pairs for different reasons.
_CAUSE_RANK: dict[VerdictCause, int] = {
    VerdictCause.TIME_DISJOINT: 0,
    VerdictCause.VERTICAL_CLEAR: 1,
    VerdictCause.HORIZONTAL_CLEAR: 2,
    VerdictCause.NEVER_SIMULTANEOUS: 3,
    VerdictCause.CONTACT: 4,
    VerdictCause.REFERENCE_MISMATCH: 5,
    VerdictCause.INVALID_GEOMETRY: 6,
    VerdictCause.EMPTY_INPUT: 7,
}


@dataclass(frozen=True)
class ProtectedSlice:
    """The exact 3D protected volume of a body at one instant.

    A cylinder: a horizontal disc of ``radius_mm`` around the centre and a
    vertical extent of ``vertical_half_mm`` above and below ``altitude_mm``, in the
    slice's own altitude reference.
    """

    at_us: Microsecond
    east_mm: Millimetre
    north_mm: Millimetre
    altitude_mm: Millimetre
    radius_mm: Millimetre
    vertical_half_mm: Millimetre
    reference: AltitudeReference

    @property
    def band(self) -> AltitudeBand:
        """Closed vertical extent ``[lo, hi]``, carried as the half-open lattice."""
        return AltitudeBand(
            self.reference,
            as_millimetres(self.altitude_mm - self.vertical_half_mm),
            as_millimetres(self.altitude_mm + self.vertical_half_mm + 1),
        )

    def contains(self, east_mm: int, north_mm: int, altitude_mm: int) -> bool:
        """Exact membership: integer squares, no square root, no tolerance."""
        dx = east_mm - self.east_mm
        dy = north_mm - self.north_mm
        dz = altitude_mm - self.altitude_mm
        return (
            dx * dx + dy * dy <= self.radius_mm * self.radius_mm
            and abs(dz) <= self.vertical_half_mm
        )


class ProtectedPrism(Protocol):
    """The surface the decision procedure needs. ``SweptPrism`` satisfies it.

    Declared as read-only properties so a frozen value object matches, and typed
    in kernel units so no caller can hand it an unvalidated integer.
    """

    @property
    def volume_id(self) -> str: ...

    @property
    def basis(self) -> SweepBasis: ...

    @property
    def reference(self) -> AltitudeReference: ...

    @property
    def authoritative(self) -> bool: ...

    @property
    def plane_origin(self) -> tuple[float, float]: ...

    @property
    def motion_start_us(self) -> Microsecond: ...

    @property
    def motion_end_us(self) -> Microsecond: ...

    @property
    def pad_us(self) -> Microsecond: ...

    @property
    def start_east_mm(self) -> Millimetre: ...

    @property
    def start_north_mm(self) -> Millimetre: ...

    @property
    def start_alt_mm(self) -> Millimetre: ...

    @property
    def end_east_mm(self) -> Millimetre: ...

    @property
    def end_north_mm(self) -> Millimetre: ...

    @property
    def end_alt_mm(self) -> Millimetre: ...

    @property
    def radius_mm(self) -> Millimetre: ...

    @property
    def vertical_half_mm(self) -> Millimetre: ...

    def window(self) -> Interval: ...

    def band(self) -> AltitudeBand: ...

    def footprint_wkt(self) -> str: ...

    def instantaneous_slice(self, at_us: int) -> ProtectedSlice: ...


@dataclass(frozen=True)
class ContactEvidence:
    """Where and when two volumes first proved to touch."""

    at_us: Microsecond
    east_mm: Millimetre
    north_mm: Millimetre
    altitude_mm: Millimetre
    centre_gap_mm: Millimetre
    vertical_gap_mm: Millimetre
    volume_ids: tuple[str, str]


@dataclass(frozen=True)
class IntersectionResult:
    """A typed, explainable verdict over time.

    ``windows`` are absolute kernel intervals in the inclusive geometric
    convention above: each is a maximal run of instants on which the volumes
    touch, merged across prism pairs and sorted.
    """

    verdict: Verdict
    cause: VerdictCause
    windows: tuple[Interval, ...]
    contacts: tuple[ContactEvidence, ...]
    authoritative: bool
    basis: SweepBasis

    @property
    def is_proof(self) -> bool:
        """True only when the answer is SAFE and every input was authoritative."""
        return self.verdict is Verdict.SAFE and self.authoritative

    def first_contact(self) -> Microsecond | None:
        return self.windows[0].start if self.windows else None


def _interp(at_us: int, t0: int, t1: int, p0: int, p1: int) -> Fraction:
    """Clamped linear interpolation, exact."""
    if at_us <= t0:
        return Fraction(p0)
    if at_us >= t1:
        return Fraction(p1)
    if t1 <= t0:
        return Fraction(p0)
    return Fraction(p0) + Fraction((at_us - t0) * (p1 - p0), t1 - t0)


def _slope(t0: int, t1: int, p0: int, p1: int) -> Fraction:
    """Exact rate per microsecond over a leg; zero for a stationary prism."""
    return Fraction(0) if t1 <= t0 else Fraction(p1 - p0, t1 - t0)


def centre_at(prism: ProtectedPrism, at_us: int) -> tuple[Fraction, Fraction, Fraction]:
    """Exact centre ``(east, north, altitude)`` of a prism at an instant."""
    return (
        _interp(
            at_us,
            prism.motion_start_us,
            prism.motion_end_us,
            prism.start_east_mm,
            prism.end_east_mm,
        ),
        _interp(
            at_us,
            prism.motion_start_us,
            prism.motion_end_us,
            prism.start_north_mm,
            prism.end_north_mm,
        ),
        _interp(
            at_us,
            prism.motion_start_us,
            prism.motion_end_us,
            prism.start_alt_mm,
            prism.end_alt_mm,
        ),
    )


def velocity_of(prism: ProtectedPrism) -> tuple[Fraction, Fraction, Fraction]:
    """The prism's leg rate per microsecond: its heading, for classification."""
    return (
        _slope(prism.motion_start_us, prism.motion_end_us, prism.start_east_mm, prism.end_east_mm),
        _slope(
            prism.motion_start_us, prism.motion_end_us, prism.start_north_mm, prism.end_north_mm
        ),
        _slope(prism.motion_start_us, prism.motion_end_us, prism.start_alt_mm, prism.end_alt_mm),
    )


def _law_over(
    prism: ProtectedPrism, start_us: int, stop_us: int
) -> tuple[tuple[Fraction, Fraction, Fraction], tuple[Fraction, Fraction, Fraction]]:
    """Centre at ``start_us`` and the rate that actually holds over that sub-window.

    Derived from the two bounding instants, so a sub-window that lies inside a
    time pad yields zero rate: the body is parked at the leg end there, not
    replaying the leg backwards.
    """
    first = centre_at(prism, start_us)
    span = stop_us - start_us
    if span <= 0:
        return first, (Fraction(0), Fraction(0), Fraction(0))
    second = centre_at(prism, stop_us)
    rate = tuple((second[axis] - first[axis]) / span for axis in range(3))
    return first, (rate[0], rate[1], rate[2])


def alive_span(prism: ProtectedPrism) -> tuple[int, int]:
    """Closed first and last instant the volume is alive, uncertainty padding included."""
    return (prism.motion_start_us - prism.pad_us, prism.motion_end_us + prism.pad_us)


def common_span(left: ProtectedPrism, right: ProtectedPrism) -> tuple[int, int] | None:
    """The instants both volumes are alive on, or ``None`` if there are none."""
    a_lo, a_hi = alive_span(left)
    b_lo, b_hi = alive_span(right)
    lo, hi = max(a_lo, b_lo), min(a_hi, b_hi)
    return (lo, hi) if lo <= hi else None


def comparable_frames(left: ProtectedPrism, right: ProtectedPrism) -> VerdictCause | None:
    """``REFERENCE_MISMATCH`` when the two volumes cannot be put in one universe."""
    if left.plane_origin != right.plane_origin:
        return VerdictCause.REFERENCE_MISMATCH
    if left.reference is not right.reference:
        return VerdictCause.REFERENCE_MISMATCH
    return None


def _breakpoints(lo: int, hi: int, left: ProtectedPrism, right: ProtectedPrism) -> tuple[int, ...]:
    """Sorted instants where either prism changes its motion law, clipped to the span.

    A sorted list of integers, never a set iteration: the order the solver walks
    sub-windows in is part of the verdict's reproducibility.
    """
    marks: set[int] = {lo, hi}
    for prism in (left, right):
        for mark in (prism.motion_start_us, prism.motion_end_us):
            if lo <= mark <= hi:
                marks.add(mark)
    return tuple(sorted(marks))


def _sub_windows(
    lo: int, hi: int, left: ProtectedPrism, right: ProtectedPrism
) -> list[tuple[int, int]]:
    """Closed integer sub-windows on which both motion laws are linear.

    A span of exactly one instant is its own sub-window: two volumes that touch
    for a single microsecond are touching, and dropping the degenerate case would
    lose every tangential contact as a false ``SAFE``.
    """
    marks = _breakpoints(lo, hi, left, right)
    if len(marks) < 2:
        return [(lo, hi)]
    return [(start, stop) for start, stop in itertools.pairwise(marks) if start <= stop]


def _first_true(lo: int, hi: int, predicate: Callable[[int], bool]) -> int:
    """Smallest true instant when the predicate is non-decreasing and ``predicate(hi)``."""
    while lo < hi:
        mid = (lo + hi) // 2
        if predicate(mid):
            hi = mid
        else:
            lo = mid + 1
    return lo


def _last_true(lo: int, hi: int, predicate: Callable[[int], bool]) -> int:
    """Largest true instant when the predicate is non-increasing and ``predicate(lo)``."""
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if predicate(mid):
            lo = mid
        else:
            hi = mid - 1
    return lo


def vertical_integer_range(
    offset: Fraction, rate: Fraction, limit: int, lo: int, hi: int
) -> tuple[int, int] | None:
    """Integer instants where ``|offset + rate*t| <= limit``, clipped to ``[lo, hi]``."""
    if limit < 0:
        return None
    if rate == 0:
        return (lo, hi) if abs(offset) <= limit else None
    lower = Fraction(-limit - offset, rate)
    upper = Fraction(limit - offset, rate)
    if lower > upper:
        lower, upper = upper, lower
    start = max(lo, math.ceil(lower))
    stop = min(hi, math.floor(upper))
    return (start, stop) if start <= stop else None


def _value_at(a: Fraction, b: Fraction, c: Fraction, t: int) -> Fraction:
    return a * t * t + b * t + c


def horizontal_sublevel(  # noqa: PLR0911 - the closed-form cases are the readability
    a: Fraction, b: Fraction, c: Fraction, lo: int, hi: int
) -> tuple[int, int] | None:
    """Run of integer instants in ``[lo, hi]`` where ``a*t^2 + b*t + c <= 0``.

    No square root is taken: the sublevel set of a convex quadratic is one run, so
    its ends come from binary search outward from the integers bracketing the
    vertex, using exact sign tests only.
    """
    if a == 0:
        if b == 0:
            return (lo, hi) if c <= 0 else None
        if b > 0:
            if _value_at(a, b, c, lo) > 0:
                return None
            return (lo, _last_true(lo, hi, lambda t: _value_at(a, b, c, t) <= 0))
        if _value_at(a, b, c, hi) > 0:
            return None
        return (_first_true(lo, hi, lambda t: _value_at(a, b, c, t) <= 0), hi)
    vertex = Fraction(-b, 2 * a)
    left_anchor = min(hi, max(lo, math.floor(vertex)))
    right_anchor = min(hi, max(lo, math.ceil(vertex)))

    def touching(t: int) -> bool:
        return _value_at(a, b, c, t) <= 0

    start: int | None = None
    stop: int | None = None
    if touching(left_anchor):
        start = _first_true(lo, left_anchor, touching)
    if touching(right_anchor):
        stop = _last_true(right_anchor, hi, touching)
    if start is None and stop is None:
        return None
    if start is None:
        start = right_anchor
    if stop is None:
        stop = left_anchor
    return (start, stop) if start <= stop else None


def _ceil_gap(dx: Fraction, dy: Fraction) -> Millimetre:
    """Cheapest integer upper bound on ``hypot(dx, dy)``: a gap, never understated."""
    squared = dx * dx + dy * dy
    if squared <= 0:
        return as_millimetres(0)
    scaled = squared.numerator * squared.denominator
    root = math.isqrt(scaled)
    micro = root if root * root == scaled else root + 1
    return as_millimetres(micro // 1000 + (1 if micro % 1000 else 0))


@dataclass(frozen=True)
class _PairScan:
    """Internal aggregate of one prism pair's evaluation."""

    windows: tuple[tuple[int, int], ...]
    has_common_span: bool
    saw_vertical_possible: bool
    saw_horizontal_possible: bool

    @property
    def safe_cause(self) -> VerdictCause:
        if not self.has_common_span:
            return VerdictCause.TIME_DISJOINT
        if not self.saw_vertical_possible:
            return VerdictCause.VERTICAL_CLEAR
        if not self.saw_horizontal_possible:
            return VerdictCause.HORIZONTAL_CLEAR
        return VerdictCause.NEVER_SIMULTANEOUS


def scan_prism_pair(left: ProtectedPrism, right: ProtectedPrism) -> _PairScan:
    """Find every run of instants on which two swept prisms touch.

    Incomparable frames produce no windows, and :func:`pair_verdict` turns that
    into ``UNPROVEN`` rather than letting a fabricated datum claim clearance.
    """
    if comparable_frames(left, right) is not None:
        return _PairScan((), False, False, False)
    span = common_span(left, right)
    if span is None:
        return _PairScan((), False, False, False)
    lo, hi = span
    reach = left.radius_mm + right.radius_mm
    vertical_limit = left.vertical_half_mm + right.vertical_half_mm
    windows: list[tuple[int, int]] = []
    saw_vertical = False
    saw_horizontal = False
    for start, stop in _sub_windows(lo, hi, left, right):
        offset = stop - start
        here, first_rate = _law_over(left, start, stop)
        there, second_rate = _law_over(right, start, stop)
        ux, uy, uz = here[0] - there[0], here[1] - there[1], here[2] - there[2]
        vx, vy, vz = (
            first_rate[0] - second_rate[0],
            first_rate[1] - second_rate[1],
            (first_rate[2] - second_rate[2]),
        )
        vertical = vertical_integer_range(uz, vz, vertical_limit, 0, offset)
        if vertical is None:
            continue
        saw_vertical = True
        a = vx * vx + vy * vy
        b = 2 * (ux * vx + uy * vy)
        c = ux * ux + uy * uy - reach * reach
        if horizontal_sublevel(a, b, c, 0, offset) is None:
            continue
        saw_horizontal = True
        run = horizontal_sublevel(a, b, c, vertical[0], vertical[1])
        if run is None:
            continue
        windows.append((start + run[0], start + run[1]))
    return _PairScan(tuple(windows), True, saw_vertical, saw_horizontal)


def merge_windows(spans: Sequence[tuple[int, int]]) -> tuple[Interval, ...]:
    """Sort and merge inclusive instant runs into canonical half-open intervals."""
    merged: list[Interval] = []
    for lo, hi in sorted(spans):
        if hi < lo:
            continue
        if merged and lo <= merged[-1].end:
            previous = merged[-1]
            merged[-1] = Interval.of(previous.start, max(previous.end, hi + 1))
        else:
            merged.append(Interval.of(lo, hi + 1))
    return tuple(merged)


def _evidence_at(left: ProtectedPrism, right: ProtectedPrism, at_us: int) -> ContactEvidence:
    ax, ay, az = centre_at(left, at_us)
    bx, by, bz = centre_at(right, at_us)
    return ContactEvidence(
        at_us=Microsecond(at_us),
        east_mm=as_millimetres(math.floor(ax)),
        north_mm=as_millimetres(math.floor(ay)),
        altitude_mm=as_millimetres(math.floor(az)),
        centre_gap_mm=_ceil_gap(ax - bx, ay - by),
        vertical_gap_mm=as_millimetres(math.ceil(abs(bz - az))),
        volume_ids=(left.volume_id, right.volume_id),
    )


def _unproven(
    left: ProtectedPrism, right: ProtectedPrism, cause: VerdictCause
) -> IntersectionResult:
    """An answer that cannot be proven. Never ``SAFE``, never an exception."""
    return IntersectionResult(
        verdict=Verdict.UNPROVEN,
        cause=cause,
        windows=(),
        contacts=(),
        authoritative=False,
        basis=left.basis if left.basis is right.basis else SweepBasis.CONTINUOUS,
    )


def _mixed_basis(left: ProtectedPrism, right: ProtectedPrism) -> SweepBasis:
    return left.basis if left.basis is right.basis else SweepBasis.CONTINUOUS


def pair_verdict(left: ProtectedPrism, right: ProtectedPrism) -> IntersectionResult:
    """Exact verdict for one pair of swept prisms, over all time."""
    blocked = comparable_frames(left, right)
    if blocked is not None:
        return _unproven(left, right, blocked)
    scan = scan_prism_pair(left, right)
    windows = merge_windows(scan.windows)
    authoritative = left.authoritative and right.authoritative
    basis = _mixed_basis(left, right)
    if windows:
        return IntersectionResult(
            verdict=Verdict.CONFLICT,
            cause=VerdictCause.CONTACT,
            windows=windows,
            contacts=tuple(_evidence_at(left, right, window.start) for window in windows),
            authoritative=authoritative,
            basis=basis,
        )
    return IntersectionResult(
        verdict=Verdict.SAFE,
        cause=scan.safe_cause,
        windows=(),
        contacts=(),
        authoritative=authoritative,
        basis=basis,
    )


def profile_verdict(
    left: Sequence[ProtectedPrism], right: Sequence[ProtectedPrism]
) -> IntersectionResult:
    """Volume-set versus volume-set: the union of every prism pair's verdict.

    Pair order is the input order, which is sweep order, and the windows are
    merged and sorted - so the answer does not depend on how the lists arrived.
    """
    if not left or not right:
        return IntersectionResult(
            verdict=Verdict.UNPROVEN,
            cause=VerdictCause.EMPTY_INPUT,
            windows=(),
            contacts=(),
            authoritative=False,
            basis=SweepBasis.CONTINUOUS,
        )
    runs: list[tuple[int, int]] = []
    found: list[ContactEvidence] = []
    causes: list[VerdictCause] = []
    unproven = False
    authoritative = True
    for prism in left:
        for other in right:
            result = pair_verdict(prism, other)
            authoritative = authoritative and result.authoritative
            if result.verdict is Verdict.UNPROVEN:
                unproven = True
                continue
            for window in result.windows:
                runs.append((window.start, window.end - 1))
            found.extend(result.contacts)
            causes.append(result.cause)
    windows = merge_windows(runs)
    ordered_contacts = tuple(sorted(found, key=lambda item: (item.at_us, item.volume_ids)))
    basis = left[0].basis
    if windows:
        return IntersectionResult(
            verdict=Verdict.CONFLICT,
            cause=VerdictCause.CONTACT,
            windows=windows,
            contacts=ordered_contacts,
            authoritative=authoritative,
            basis=basis,
        )
    if unproven:
        return IntersectionResult(
            verdict=Verdict.UNPROVEN,
            cause=VerdictCause.REFERENCE_MISMATCH,
            windows=(),
            contacts=(),
            authoritative=False,
            basis=basis,
        )
    weakest = (
        max(causes, key=lambda cause: _CAUSE_RANK[cause]) if causes else VerdictCause.EMPTY_INPUT
    )
    return IntersectionResult(
        verdict=Verdict.SAFE,
        cause=weakest,
        windows=(),
        contacts=(),
        authoritative=authoritative,
        basis=basis,
    )


def volume_pair_verdict(left: ProtectedPrism, right: ProtectedPrism) -> IntersectionResult:
    """Conservative volume-versus-volume test: footprint, band and window only.

    It ignores *when inside the window* each volume is where, so it is a superset
    of :func:`pair_verdict`. That is what makes it safe as a pre-filter and as the
    slow, conservative answer: it may report a conflict the exact sweep then rules
    out, and it may report ``UNPROVEN`` where geometry cannot be trusted, but it
    never calls a real contact safe.
    """
    blocked = comparable_frames(left, right)
    if blocked is not None:
        return _unproven(left, right, blocked)
    left_footprint = left.footprint_wkt()
    right_footprint = right.footprint_wkt()
    if not (shapes.is_usable(left_footprint) and shapes.is_usable(right_footprint)):
        return _unproven(left, right, VerdictCause.INVALID_GEOMETRY)
    windows = (
        Interval.of(
            max(left.window().start, right.window().start),
            min(left.window().end, right.window().end),
        )
        if left.window().overlaps(right.window())
        else None
    )
    band_overlap = left.band().intersects(right.band())
    footprint_overlap = (
        windows is not None
        and band_overlap
        and shapes.shapes_overlap(left_footprint, right_footprint)
    )
    if footprint_overlap and windows is not None:
        return IntersectionResult(
            verdict=Verdict.CONFLICT,
            cause=VerdictCause.CONTACT,
            windows=(windows,),
            contacts=(_evidence_at(left, right, windows.start),),
            authoritative=left.authoritative and right.authoritative,
            basis=_mixed_basis(left, right),
        )
    cause = (
        VerdictCause.TIME_DISJOINT
        if windows is None
        else (VerdictCause.VERTICAL_CLEAR if not band_overlap else VerdictCause.HORIZONTAL_CLEAR)
    )
    return IntersectionResult(
        verdict=Verdict.SAFE,
        cause=cause,
        windows=(),
        contacts=(),
        authoritative=left.authoritative and right.authoritative,
        basis=_mixed_basis(left, right),
    )
