"""The four inspections, and the pair and batch verdicts they produce.

``technical-build/11`` section 7 lists what an independent checker has to look at,
and this module answers that list one inspection at a time so a disagreement can be
attributed rather than argued about:

``CONTINUOUS_SWEEP``
    every pair of position pieces whose absolute-time windows overlap, decided by
    the lateral distance of a *relative track* from the origin. No step size is
    involved, so nothing can pass between samples.
``TRANSITION_BUFFER``
    the same sweep restricted to the padded head or tail of a leg, reported
    separately because a contact there is not a collision course - it is a plan whose
    manoeuvre slack runs into somebody else's, and it is the failure the buffer exists
    to catch.
``ADJACENT_CORRIDOR``
    a deliberately time-blind test: do these two lanes, or two lanes declared
    adjacent, come within each other's protection at all while both plans' padded
    windows and vertical extents overlap. It ignores *where along each lane* each
    aircraft is, which makes it coarser than the sweep on purpose.
``CROSSING_VOLUME``
    where the two horizontal tracks cross, and how far apart their arrivals at that
    shared point are.

The result of a pair is the worst of the four, and the worst of a batch is the worst
of its pairs. There is no averaging, no voting, and no "the planner said safe so we
will let it through": :class:`Disagreement` is a release blocker in both directions,
including the direction where the checker went softer than the planner, which is the
one an organisation is most likely to file as informational and forget.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from fractions import Fraction
from typing import Final, Literal

from droneops_geom_core import GeomCoreError, Interval

from . import geometry
from .policy import AllowRuleSet, CheckerPolicy
from .records import (
    CommittedPlanRecord,
    EvaluationContext,
    altitude_reference_of,
    cell_token_of,
    validate_record,
)
from .sweep import PairFrame, PieceKind, PositionPiece, SweepError, SweptPlan, sweep_plan
from .verdict import (
    AdjacencyFinding,
    AllowGrant,
    CheckerMetrics,
    CheckName,
    Contact,
    CrossingFinding,
    PairVerdict,
    ReasonCode,
    UnprovenReason,
    Verdict,
    worst,
)

Basis = Literal["CONTINUOUS", "DISCRETISED", "FRAME"]

#: The three bases, in the order a report lists them.
BASES: Final[tuple[Basis, ...]] = ("CONTINUOUS", "DISCRETISED", "FRAME")

#: What ``min_*_gap_mm`` means when the pair never got far enough to measure one.
_NOT_MEASURED: Final[float] = 1.0e30


@dataclass(frozen=True)
class Violation:
    """One pair that may not be released, and the whole case against it."""

    subject: str
    verdict: str
    cause: str
    contacts: tuple[Contact, ...]
    reasons: tuple[UnprovenReason, ...]
    grant: AllowGrant | None
    contested_cell: str | None
    first_contact_us: int | None

    @property
    def blocked(self) -> bool:
        """An allow rule can excuse a measured contact; it can never excuse an unknown.

        ``UNPROVEN`` is not a conflict the policy could waive: an exception says "this
        close approach is acceptable", and it has nothing to say about a record the
        checker could not evaluate at all. So ``UNPROVEN`` blocks, full stop.
        """
        if self.verdict == "SAFE":
            return False
        if self.verdict == "UNPROVEN":
            return True
        return self.grant is None or not self.grant.within_envelope

    def explain(self) -> str:
        head = f"{self.subject}: {self.verdict} ({self.cause})"
        if self.grant is None:
            return head
        if self.blocked:
            return (
                f"{head} - allow rule {self.grant.rule_id}@{self.grant.rule_version} does "
                "not cover the measured contact"
            )
        return f"{head} - excepted by {self.grant.rule_id}@{self.grant.rule_version}"


@dataclass(frozen=True)
class Disagreement:
    """Planner and checker gave different answers about the same committed pair.

    Recorded, never reconciled. There is no code path in this package that takes a
    checker ``CONFLICT`` and a planner ``SAFE`` and produces ``SAFE``: the pair goes
    into the report with ``blocks_release`` true and stays there, because the moment a
    validator may downgrade its own finding the second implementation stops being
    evidence (conventions section 6).
    """

    subject: str
    planner_verdict: str
    checker_verdict: str
    kind: str
    note: str
    blocks_release: bool = True


@dataclass(frozen=True)
class ConflictReport:
    """The batch answer over a committed reservation set."""

    records_checked: int
    pairs_checked: int
    verdict: str
    violations: tuple[Violation, ...]
    disagreements: tuple[Disagreement, ...]
    defects: tuple[tuple[str, tuple[UnprovenReason, ...]], ...]
    policy_key: str
    policy_authoritative: bool
    evaluated_at: datetime
    basis: Basis

    @property
    def blocks_release(self) -> bool:
        """Any unexcused conflict, any disagreement, or any record that could not be checked."""
        return (
            any(item.blocked for item in self.violations)
            or any(item.blocks_release for item in self.disagreements)
            or bool(self.defects)
        )

    @property
    def unexcused(self) -> tuple[Violation, ...]:
        """The violations no active allow rule covers."""
        return tuple(item for item in self.violations if item.blocked)

    def release_blockers(self) -> tuple[str, ...]:
        """Every reason the set may not be released, sorted, in one list."""
        found: list[str] = [item.explain() for item in self.violations if item.blocked]
        found.extend(
            f"DISAGREEMENT {item.subject}: planner {item.planner_verdict} / checker "
            f"{item.checker_verdict} ({item.kind})"
            for item in self.disagreements
            if item.blocks_release
        )
        found.extend(
            f"UNVERIFIABLE RECORD {plan}: "
            + "; ".join(reason.as_text() for reason in reasons)
            for plan, reasons in self.defects
        )
        return tuple(sorted(found))

    @property
    def certified(self) -> bool:
        """A clean answer under a non-authoritative policy is still a simulation answer.

        ``authoritative: false`` in the policy file is a statement about the *margins*,
        so it cannot change a verdict - but no release may describe this report as a
        certified clearance, and that is what this property exists to stop.
        """
        return not self.blocks_release and self.policy_authoritative

    def summary(self) -> str:
        conflicts = sum(1 for item in self.violations if item.verdict == "CONFLICT")
        unproven = sum(1 for item in self.violations if item.verdict == "UNPROVEN")
        return (
            f"{self.verdict} over {self.records_checked} record(s) and {self.pairs_checked} "
            f"pair(s): {conflicts} conflict, {unproven} unproven, {len(self.disagreements)} "
            f"disagreement(s); release {'BLOCKED' if self.blocks_release else 'clear'}"
        )


# --------------------------------------------------------------------------- pair verdict
def check_pair(
    left: CommittedPlanRecord,
    right: CommittedPlanRecord,
    context: EvaluationContext,
    *,
    basis: Basis = "CONTINUOUS",
    step_us: int | None = None,
    rules: AllowRuleSet | None = None,
) -> PairVerdict:
    """The checker's full answer about one pair of committed plans.

    ``basis`` selects which construction the answer rests on, and the three are not
    interchangeable: ``CONTINUOUS`` is the sweep, ``DISCRETISED`` is the inflated
    sampled cover that contains it, and ``FRAME`` is the naive per-instant sample whose
    agreement with the other two proves nothing. ``FRAME`` runs the sweep test alone, so
    that its disagreement with ``CONTINUOUS`` is a statement about sampling and about
    nothing else.
    """
    subject = pair_subject(left, right)
    reasons = _record_reasons(left, right, context)
    frame: PairFrame | None = None
    sweeps: tuple[SweptPlan, SweptPlan] | None = None
    if not reasons:
        frame, sweeps, build_reasons = _sweeps(left, right, context, basis=basis, step_us=step_us)
        reasons.extend(build_reasons)

    if sweeps is None or frame is None:
        return PairVerdict(
            subject=subject,
            verdict="UNPROVEN",
            cause="UNPROVEN_INPUT",
            reasons=tuple(sorted(reasons, key=UnprovenReason.sort_key)),
            contacts=(),
            crossings=(),
            corridors=(),
            metrics=CheckerMetrics.empty(),
        )

    left_sweep, right_sweep = sweeps
    swept, tested, legs, min_horizontal, min_vertical = _piece_contacts(
        left_sweep, right_sweep, basis
    )
    contacts = tuple(swept)
    crossings: tuple[CrossingFinding, ...] = ()
    corridors: tuple[AdjacencyFinding, ...] = ()
    if basis != "FRAME":
        crossings, crossing_contacts = _crossing_contacts(left_sweep, right_sweep, context.policy)
        corridors, corridor_contacts = _corridor_findings(left_sweep, right_sweep, context)
        contacts = contacts + crossing_contacts + corridor_contacts
    contacts = tuple(sorted(contacts, key=Contact.sort_key))

    metrics = CheckerMetrics(
        legs_compared=legs,
        piece_pairs_tested=tested,
        contact_windows=len(contacts),
        contact_span_us=sum(contact.span_us for contact in contacts),
        crossing_points=len(crossings),
        corridor_pairs=len(corridors),
        min_horizontal_gap_mm=min_horizontal,
        min_vertical_gap_mm=min_vertical,
        unproven_reasons=len(reasons),
    )
    grant = grant_for(context, rules, left, right, contacts)
    contested = _contested_cell(contacts, frame, context)

    if contacts:
        verdict: Literal["SAFE", "CONFLICT", "UNPROVEN"] = "CONFLICT"
        cause = "CONTACT"
    elif reasons:
        verdict, cause = "UNPROVEN", "UNPROVEN_INPUT"
    else:
        verdict, cause = "SAFE", _safe_cause(crossings, corridors)
    return PairVerdict(
        subject=subject,
        verdict=verdict,
        cause=cause,
        reasons=tuple(sorted(reasons, key=UnprovenReason.sort_key)),
        contacts=contacts,
        crossings=crossings,
        corridors=corridors,
        metrics=metrics,
        grant=grant,
        contested_cell=contested,
    )


def pair_subject(left: CommittedPlanRecord, right: CommittedPlanRecord) -> str:
    """A canonical pair label, identical whichever argument came first."""
    return "|".join(sorted((left.plan_version or "-", right.plan_version or "-")))


def _record_reasons(
    left: CommittedPlanRecord, right: CommittedPlanRecord, context: EvaluationContext
) -> list[UnprovenReason]:
    """Everything wrong with either record, plus what wrong with *both* of them."""
    reasons = list(validate_record(left, context)) + list(validate_record(right, context))
    if not left.holds_airspace or not right.holds_airspace:
        for record in (left, right):
            if not record.holds_airspace:
                reasons.append(
                    UnprovenReason(
                        ReasonCode.NOT_A_COMMITTED_RECORD,
                        f"state {record.status!r} holds no airspace, so there is no pair to check",
                        record.plan_version,
                    )
                )
    frames = {altitude_reference_of(left), altitude_reference_of(right)}
    if any(frame is None for frame in frames):
        for record in (left, right):
            if altitude_reference_of(record) is None and record.points:
                reasons.append(
                    UnprovenReason(
                        ReasonCode.MISSING_ALTITUDE_REFERENCE,
                        "the plan does not state one altitude reference for all its points",
                        record.plan_version,
                    )
                )
    elif len(frames) > 1:
        reasons.append(
            UnprovenReason(
                ReasonCode.CROSS_DATUM_COMPARISON,
                "the pair states " + ", ".join(sorted(
                    frame.value for frame in frames if frame is not None
                )),
                pair_subject(left, right),
            )
        )
    return sorted(set(reasons), key=UnprovenReason.sort_key)


def _sweeps(
    left: CommittedPlanRecord,
    right: CommittedPlanRecord,
    context: EvaluationContext,
    *,
    basis: Basis,
    step_us: int | None,
) -> tuple[PairFrame | None, tuple[SweptPlan, SweptPlan] | None, list[UnprovenReason]]:
    """Both plans on one projected frame, with a reason per record that cannot be swept.

    Each record is swept on its own and reports its own defect, rather than one shared
    ``try`` that would attribute the failure to whichever record happened to be
    evaluated first. An answer that changed when the arguments were swapped would be a
    answer about argument order, and the symmetry property would be false.
    """
    reasons: list[UnprovenReason] = []
    try:
        frame = PairFrame.of(left, right)
    except SweepError as error:
        return None, None, [
            UnprovenReason(ReasonCode.PLANAR_EXTENT_EXCEEDED, str(error), pair_subject(left, right))
        ]
    step = step_us if basis != "CONTINUOUS" else None
    built: list[SweptPlan] = []
    for record in (left, right):
        try:
            built.append(
                sweep_plan(record, frame, context.policy, basis_step_us=step,
                           inflate=basis != "FRAME")
            )
        except SweepError as error:
            reasons.append(
                UnprovenReason(
                    ReasonCode.MALFORMED_PROFILE, str(error), record.plan_version or "-"
                )
            )
    if len(built) != 2:
        return frame, None, reasons
    return frame, (built[0], built[1]), reasons


# ------------------------------------------------------------------------- the sweep test
def _piece_contacts(
    left: SweptPlan, right: SweptPlan, basis: Basis
) -> tuple[list[Contact], int, int, float, float]:
    """Every proven contact between two swept plans, plus what the test cost.

    For each pair of overlapping absolute-time windows the two centres have a *linear*
    relative track, so one shapely distance query answers "did they ever get laterally
    close" for every instant in the window at once. Where they did, the vertical
    separation over that same sub-window is a linear function of time whose minimum is
    exact, so the two axes are coupled *in time* rather than multiplied together as
    independent bounds. That coupling is the difference between a sweep and a bounding
    box, and it is why a contact can be reported to the microsecond.
    """
    contacts: list[Contact] = []
    tested = 0
    legs = 0
    min_horizontal = _NOT_MEASURED
    min_vertical = _NOT_MEASURED
    for first in left.sorted_pieces():
        for second in right.sorted_pieces():
            shared = _shared_window(first.window(), second.window())
            if shared is None:
                continue
            low, high = shared
            tested += 1
            legs = max(legs, first.leg_index + 1, second.leg_index + 1)
            gap, vertical = _closest_separation(first, second, low, high)
            contact = _contact_of(first, second, low, high, basis)
            if contact is None:
                min_horizontal = min(min_horizontal, gap)
                min_vertical = min(min_vertical, vertical)
                continue
            contacts.append(contact)
            min_horizontal = min(min_horizontal, contact.horizontal_gap_mm)
            min_vertical = min(min_vertical, contact.vertical_gap_mm)
    return contacts, tested, legs, min_horizontal, min_vertical


def _shared_window(first: Interval, second: Interval) -> tuple[int, int] | None:
    """The inclusive whole-microsecond range on which both pieces are alive."""
    overlap = first.intersection(second)
    if not overlap:
        return None
    low, high = overlap[0].start, overlap[0].end - 1
    return (low, high) if high >= low else None


def _relative_track(
    first: PositionPiece, second: PositionPiece, low: int, high: int
) -> tuple[tuple[float, float], tuple[float, float], Fraction, Fraction]:
    """Relative centres at both ends of the window, and the height difference at both."""
    a_start, a_end = first.centre_at(low), first.centre_at(high)
    b_start, b_end = second.centre_at(low), second.centre_at(high)
    return (
        (float(a_start[0] - b_start[0]), float(a_start[1] - b_start[1])),
        (float(a_end[0] - b_end[0]), float(a_end[1] - b_end[1])),
        a_start[2] - b_start[2],
        a_end[2] - b_end[2],
    )


def _closest_separation(
    first: PositionPiece, second: PositionPiece, low: int, high: int
) -> tuple[float, float]:
    """The closest approach reported for a window that did *not* contact."""
    rel_start, rel_end, height_start, height_end = _relative_track(first, second, low, high)
    gap = geometry.segment_gap_mm(rel_start, rel_end)
    return gap, _round_up(float(_min_abs_linear(height_start, height_end, low, high)))


def _contact_of(
    first: PositionPiece, second: PositionPiece, low: int, high: int, basis: Basis
) -> Contact | None:
    """Decide one overlapping window: a contact, or nothing at all."""
    rel_start, rel_end, height_start, height_end = _relative_track(first, second, low, high)
    reach = float(first.reach_mm + second.reach_mm)
    gap = geometry.segment_gap_mm(rel_start, rel_end)
    if gap > reach:
        return None
    span = geometry.segment_contact_span(rel_start, rel_end, reach, low, high)
    if span is None:
        return None
    contact_low, contact_high = span
    vertical = _min_abs_linear(height_start, height_end, contact_low, contact_high)
    if float(vertical) > float(first.vertical_half_mm + second.vertical_half_mm):
        return None
    at_us = geometry.closest_approach_us(rel_start, rel_end, low, high)
    east, north, altitude = first.centre_at(at_us)
    return Contact(
        at_us=at_us,
        window_start_us=contact_low,
        window_end_us=contact_high,
        east_mm=int(math.floor(east)),
        north_mm=int(math.floor(north)),
        altitude_mm=int(math.floor(altitude)),
        horizontal_gap_mm=gap,
        vertical_gap_mm=_round_up(float(abs(vertical))),
        check=_attributed_check(first, second, basis),
        legs=(first.leg_index, second.leg_index),
    )


def _attributed_check(first: PositionPiece, second: PositionPiece, basis: Basis) -> CheckName:
    """Which inspection found this, so a fix can be aimed at the right margin."""
    if basis == "FRAME":
        return CheckName.FRAME_SAMPLE
    if first.kind is not PieceKind.MOTION or second.kind is not PieceKind.MOTION:
        return CheckName.TRANSITION_BUFFER
    return CheckName.CONTINUOUS_SWEEP


def _round_up(value: float) -> float:
    """Report a distance no smaller than the truth.

    Same rule as the geometry module, applied to the checker's own arithmetic: a
    separation may be overstated and may never be understated, so an approximation
    can only ever widen a conflict and can never narrow a clearance.
    """
    if math.isinf(value) or math.isnan(value):
        return value
    return float(math.ceil(value * 1000.0) / 1000.0)


def _min_abs_linear(start: Fraction, end: Fraction, low: int, high: int) -> Fraction:
    """Minimum absolute value of a linear function over a closed integer range.

    Exact, because a linear function on an interval is smallest in absolute value either
    at an endpoint or where it crosses zero. No step size to argue about, no epsilon to
    tune, and therefore no way for the answer to move between two runs.
    """
    if high <= low:
        return abs(start)
    if (start <= 0 <= end) or (end <= 0 <= start):
        return Fraction(0)
    return min(abs(start), abs(end))


# ------------------------------------------------------------------ crossing volume test
def _crossing_contacts(
    left: SweptPlan, right: SweptPlan, policy: CheckerPolicy
) -> tuple[tuple[CrossingFinding, ...], tuple[Contact, ...]]:
    """Where the tracks cross, and whether both bodies get there at the same time.

    Arrival times are read off each plan's own legs by projecting the crossing point onto
    the nearest segment, which clamps a point a plan never reaches to that plan's window
    edge. A crossing finding therefore cannot invent an instant the plan does not contain;
    it can only report one no earlier than it was, which is why this is a checker test and
    not a planner one. Each side contributes its own margin: timing uncertainty always,
    plus the transition buffer for whichever side is arriving on a leg that changes height.
    """
    findings: list[CrossingFinding] = []
    contacts: list[Contact] = []
    for east, north in geometry.polyline_crossings(left.float_track(), right.float_track()):
        arrival_left = _arrival(left, east, north)
        arrival_right = _arrival(right, east, north)
        if arrival_left is None or arrival_right is None:
            continue
        tolerance = policy.crossing_tolerance_us(arrival_left[1], arrival_right[1])
        gap = abs(arrival_left[0] - arrival_right[0])
        findings.append(
            CrossingFinding(
                east_mm=int(math.floor(east)),
                north_mm=int(math.floor(north)),
                left_arrival_us=arrival_left[0],
                right_arrival_us=arrival_right[0],
                arrival_gap_us=gap,
                tolerance_us=tolerance,
            )
        )
        contested = gap <= tolerance
        if contested:
            alt_left = _altitude_at(left, arrival_left[0])
            alt_right = _altitude_at(right, arrival_right[0])
            vertical_reach = _vertical_reach(left) + _vertical_reach(right)
            contested = abs(alt_left - alt_right) <= vertical_reach
        if not contested:
            continue
        first = min(arrival_left[0], arrival_right[0])
        contacts.append(
            Contact(
                at_us=first,
                window_start_us=first,
                window_end_us=first + gap,
                east_mm=int(math.floor(east)),
                north_mm=int(math.floor(north)),
                altitude_mm=_altitude_at(left, arrival_left[0]),
                horizontal_gap_mm=0.0,
                vertical_gap_mm=_round_up(float(gap) / 1000.0),
                check=CheckName.CROSSING_VOLUME,
                legs=(-1, -1),
            )
        )
    return (
        tuple(sorted(findings, key=CrossingFinding.sort_key)),
        tuple(sorted(contacts, key=Contact.sort_key)),
    )


def _altitude_at(plan: SweptPlan, at_us: int) -> int:
    """The plan's reported height at an instant, from the piece alive then."""
    for piece in plan.sorted_pieces():
        if piece.start_us <= at_us <= piece.end_us:
            return int(math.floor(piece.centre_at(at_us)[2]))
    return int(math.floor(plan.pieces[0].alt_start))


def _arrival(plan: SweptPlan, east: float, north: float) -> tuple[int, bool] | None:
    """When this plan's track is at a given planar point, and whether that leg manoeuvres.

    The second half of the answer is the policy's own manoeuvre test applied to the leg
    the body arrives on: a plan that crosses the point while changing height may be late
    by the transition buffer as well as by timing uncertainty, and one that crosses it
    level may not.
    """
    if len(plan.track_mm) < 2:
        return None
    legs = [piece for piece in plan.sorted_pieces() if piece.kind is PieceKind.MOTION]
    if not legs:
        legs = list(plan.sorted_pieces())
    best: tuple[float, int, bool] | None = None
    for piece in legs:
        distance, fraction = _point_along(
            (piece.east_start, piece.north_start), (piece.east_end, piece.north_end), (east, north)
        )
        arrival = int(round(piece.start_us + fraction * (piece.end_us - piece.start_us)))
        if best is None or distance < best[0]:
            best = (distance, arrival, piece.alt_end != piece.alt_start)
    return (best[1], best[2]) if best is not None else None


def _point_along(
    start: tuple[int, int], end: tuple[int, int], point: tuple[float, float]
) -> tuple[float, float]:
    """Distance from a point to a track segment, and where along it it projects."""
    delta_x = float(end[0] - start[0])
    delta_y = float(end[1] - start[1])
    squared = delta_x * delta_x + delta_y * delta_y
    if squared <= 0.0:
        return _round_up(math.hypot(start[0] - point[0], start[1] - point[1])), 0.0
    fraction = ((point[0] - start[0]) * delta_x + (point[1] - start[1]) * delta_y) / squared
    clamped = 0.0 if fraction < 0.0 else (1.0 if fraction > 1.0 else fraction)
    near_x = start[0] + clamped * delta_x
    near_y = start[1] + clamped * delta_y
    return _round_up(math.hypot(near_x - point[0], near_y - point[1])), clamped


# ------------------------------------------------------------- adjacent corridor test
def _corridor_findings(
    left: SweptPlan, right: SweptPlan, context: EvaluationContext
) -> tuple[tuple[AdjacencyFinding, ...], tuple[Contact, ...]]:
    """Shared or adjacent lanes, judged without looking at where along them bodies are.

    This is the over-approximation layer. It is guaranteed to flag everything the sweep
    flags on those lanes and may flag more; a contact found here and not by the sweep is
    not a defect to be reconciled, it is the case where the timing happened to work out
    while the lanes were never meant to be flown together. The exit criterion says that
    needs an explicit allow rule the independent checker also agrees with.
    """
    reach = _planar_reach(left) + _planar_reach(right)
    overlap = _shared_window(left.window, right.window)
    gap = geometry.polyline_gap_mm(left.float_track(), right.float_track())
    bands_touch = _bands_overlap(left, right)
    findings: list[AdjacencyFinding] = []
    contacts: list[Contact] = []
    for lane_left in left.corridor_keys:
        for lane_right in right.corridor_keys:
            relation = _relation(lane_left, lane_right, context)
            if relation is None:
                continue
            finding = AdjacencyFinding(
                left_key=lane_left,
                right_key=lane_right,
                relation=relation,
                planar_gap_mm=gap,
                windows_overlap=overlap is not None,
                bands_overlap=bands_touch,
                reach_mm=reach,
            )
            findings.append(finding)
            if not finding.contested or overlap is None:
                continue
            contacts.append(
                Contact(
                    at_us=overlap[0],
                    window_start_us=overlap[0],
                    window_end_us=overlap[1],
                    east_mm=int(math.floor(left.float_track()[0][0])),
                    north_mm=int(math.floor(left.float_track()[0][1])),
                    altitude_mm=_altitude_at(left, overlap[0]),
                    horizontal_gap_mm=gap,
                    vertical_gap_mm=0.0,
                    check=CheckName.ADJACENT_CORRIDOR,
                    legs=(-1, -1),
                )
            )
    return (
        tuple(sorted(findings, key=AdjacencyFinding.sort_key)),
        tuple(sorted(contacts, key=Contact.sort_key)),
    )


def _relation(lane_left: str, lane_right: str, context: EvaluationContext) -> str | None:
    """``SHARED``, ``ADJACENT``, or not related at all - from the claimed keys and the map."""
    if lane_left == lane_right:
        return "SHARED"
    left_id = lane_left.partition(":")[2]
    right_id = lane_right.partition(":")[2]
    if right_id in context.neighbours_of(left_id) or left_id in context.neighbours_of(right_id):
        return "ADJACENT"
    return None


def _planar_reach(plan: SweptPlan) -> int:
    """The widest lateral protection any piece of this plan carries."""
    return max(piece.reach_mm for piece in plan.pieces)


def _vertical_reach(plan: SweptPlan) -> int:
    """The tallest vertical protection any piece of this plan carries."""
    return max(piece.vertical_half_mm for piece in plan.pieces)


def _vertical_extent(plan: SweptPlan) -> tuple[int, int]:
    """The plan's whole-flight vertical extent, padded by its own reach, half-open."""
    lows = [min(piece.alt_start, piece.alt_end) - piece.vertical_half_mm for piece in plan.pieces]
    highs = [max(piece.alt_start, piece.alt_end) + piece.vertical_half_mm for piece in plan.pieces]
    return min(lows), max(highs) + 1


def _bands_overlap(left: SweptPlan, right: SweptPlan) -> bool:
    """Whether the two plans' entire vertical extents touch at all."""
    low_left, high_left = _vertical_extent(left)
    low_right, high_right = _vertical_extent(right)
    return low_left < high_right and low_right < high_left


def _safe_cause(crossings: Sequence[CrossingFinding], corridors: Sequence[AdjacencyFinding]) -> str:
    """What cleared a SAFE, stated as the weakest thing that had to be proven.

    Time first, then height and lateral distance, then crossing timing: a pair on the
    same lane at different minutes is cleared by the calendar and it is misleading to
    say it was cleared by separation, because moving them both ten seconds earlier would
    put them in contact. That distinction is the difference between "same segment at
    different times" being a real SAFE and being an accident.
    """
    if corridors and not any(finding.contested for finding in corridors):
        if not all(finding.windows_overlap for finding in corridors):
            return "TIME_DISJOINT"
        return "LATERAL_CLEAR"
    if crossings:
        return "CROSSING_TIMING_CLEAR"
    return "TIME_DISJOINT"


def _contested_cell(
    contacts: Sequence[Contact], frame: PairFrame, context: EvaluationContext
) -> str | None:
    """Name the volume cell a conflict is inside, in the ledger's own vocabulary.

    A conflict that cannot say which reserved volume it is about is an argument, not a
    finding - so the checker resolves the contact back through ``geom-core``'s grid and
    reports the cell token. ``None`` when the run froze no grid or the point fell outside
    the projection, neither of which is a reason to withhold the conflict itself.
    """
    if not contacts or context.grid is None:
        return None
    contact = min(contacts, key=Contact.sort_key)
    try:
        return cell_token_of(
            context,
            frame.origin,
            contact.east_mm,
            contact.north_mm,
            contact.altitude_mm,
            context.policy.altitude_reference,
        )
    except GeomCoreError:
        return None


# --------------------------------------------------------------------------- allow rules
def grant_for(
    context: EvaluationContext,
    rules: AllowRuleSet | None,
    left: CommittedPlanRecord,
    right: CommittedPlanRecord,
    contacts: Sequence[Contact],
) -> AllowGrant | None:
    """An exception only when *both* sides name the same rule and the geometry fits it.

    Four independent conditions must all hold, and all four are re-checked here rather
    than assumed: the rule exists at the id *and* version both records cite; it is in
    force and its policy release is the active one; it covers every inspection that found
    a contact; and the contact the checker measured is inside the rule's own duration and
    separation envelope. A rule cannot waive its own floor, and it cannot cover a pair
    the pair never named.
    """
    if rules is None or not rules.rules or not contacts:
        return None
    first, second = left.planner_claim, right.planner_claim
    if first is None or second is None:
        return None
    if first.allow_rule_id is None or second.allow_rule_id is None:
        return None
    if first.allow_rule_id != second.allow_rule_id:
        return None
    if first.allow_rule_version != second.allow_rule_version:
        return None
    rule = rules.find(first.allow_rule_id, first.allow_rule_version)
    if rule is None or not rule.in_force(context.evaluated_at):
        return None
    if rule.policy_version != context.policy.policy_version:
        return None
    if not rule.covers_pair(left.plan_version, right.plan_version):
        return None
    if not rule.covers_checks(sorted({contact.check.value for contact in contacts})):
        return None
    if not rule.covers_corridors(sorted(_identifiers(left) + _identifiers(right))):
        return None
    return AllowGrant(
        rule_id=rule.rule_id,
        rule_version=rule.rule_version,
        policy_version=rule.policy_version,
        named_by=(left.plan_version, right.plan_version),
        measured_contact_span_us=max(contact.span_us for contact in contacts),
        max_contact_span_us=rule.max_contact_span_us,
        measured_min_separation_mm=min(_separation_mm(contact) for contact in contacts),
        floor_mm=rule.min_separation_mm,
    )


def _identifiers(record: CommittedPlanRecord) -> list[str]:
    try:
        return [*record.corridor_identifiers(), *record.all_identifiers()]
    except GeomCoreError:
        return []


def _separation_mm(contact: Contact) -> float:
    """A lower bound on true 3D separation, so a grant can only be too hard to earn."""
    return _round_up(math.hypot(contact.horizontal_gap_mm, contact.vertical_gap_mm))


# --------------------------------------------------------------------------------- batch
def check_set(
    records: Sequence[CommittedPlanRecord],
    context: EvaluationContext,
    *,
    basis: Basis = "CONTINUOUS",
    step_us: int | None = None,
    rules: AllowRuleSet | None = None,
) -> ConflictReport:
    """Every pair of a committed reservation set, and the full violation list.

    The Phase 2 exit criterion is a statement about a *set* - no two committed
    reservations overlap under the active policy unless an explicit allow rule and the
    independent checker agree - so this enumerates the set, keeps records in canonical
    order, pairs each one against the records after it, and reports each pair once.

    A record the checker cannot evaluate is never dropped: it appears in ``defects`` and
    it blocks the release, because "we could not check row 7" is not the statement
    "row 7 is clear", and only the second one releases airspace.
    """
    holding = sorted(
        (record for record in records if record.holds_airspace),
        key=lambda record: (record.plan_version, record.mission_id),
    )
    defects = tuple(
        sorted(
            ((record.plan_version, validate_record(record, context)) for record in holding
             if validate_record(record, context)),
            key=lambda item: item[0],
        )
    )
    violations: list[Violation] = []
    disagreements: list[Disagreement] = []
    verdicts: list[Verdict] = []
    for index, left in enumerate(holding):
        for right in holding[index + 1:]:
            answer = check_pair(left, right, context, basis=basis, step_us=step_us, rules=rules)
            verdicts.append(answer.verdict)
            if answer.verdict != "SAFE":
                violations.append(_violation_of(answer))
            disagreement = _disagreement_of(left, right, answer)
            if disagreement is not None:
                disagreements.append(disagreement)
    return ConflictReport(
        records_checked=len(holding),
        pairs_checked=len(verdicts),
        verdict=worst(verdicts),
        violations=tuple(sorted(violations, key=lambda item: item.subject)),
        disagreements=tuple(sorted(disagreements, key=lambda item: item.subject)),
        defects=defects,
        policy_key=context.policy.key(),
        policy_authoritative=context.policy.authoritative,
        evaluated_at=context.evaluated_at,
        basis=basis,
    )


def _violation_of(answer: PairVerdict) -> Violation:
    return Violation(
        subject=answer.subject,
        verdict=answer.verdict,
        cause=answer.cause,
        contacts=answer.contacts,
        reasons=answer.reasons,
        grant=answer.grant,
        contested_cell=answer.contested_cell,
        first_contact_us=answer.first_contact_us,
    )


def _disagreement_of(
    left: CommittedPlanRecord, right: CommittedPlanRecord, answer: PairVerdict
) -> Disagreement | None:
    """Planner versus checker, surfaced exactly as found.

    Both directions block. Planner-safe-checker-conflict is the case the invariant is
    written for; planner-conflict-checker-safe is the case where the validator went
    softer than the thing it is supposed to validate, and it is the one that gets
    ignored if anything is allowed to be ignored.
    """
    claims = [claim for claim in (left.planner_claim, right.planner_claim) if claim is not None]
    if answer.grant is not None and answer.grant.within_envelope:
        named = {
            (claim.allow_rule_id, claim.allow_rule_version) for claim in claims
        }
        agreed = {(answer.grant.rule_id, answer.grant.rule_version)}
        if named == agreed:
            return None
    if not claims:
        return Disagreement(
            subject=answer.subject,
            planner_verdict="ABSENT",
            checker_verdict=answer.verdict,
            kind="NO_PLANNER_CLAIM",
            note="neither plan recorded a planner verdict, so the pair has one answer, not two",
        )
    for claim in claims:
        if claim.verdict == answer.verdict:
            continue
        if claim.verdict == "SAFE":
            kind = (
                "PLANNER_SAFE_CHECKER_CONFLICT"
                if answer.verdict == "CONFLICT"
                else "PLANNER_SAFE_CHECKER_UNPROVEN"
            )
            note = "the checker rejects a pair the planner accepted: release blocker, " \
                   "never reconciled in the planner's favour"
        elif claim.verdict == "CONFLICT" and answer.verdict == "SAFE":
            kind = "PLANNER_CONFLICT_CHECKER_SAFE"
            note = "the checker is softer than the planner, which is the wrong way round"
        else:
            kind = "PLANNER_UNPROVEN_CHECKER_DECIDED"
            note = "the planner declined to answer and the checker did"
        return Disagreement(
            subject=answer.subject,
            planner_verdict=claim.verdict,
            checker_verdict=answer.verdict,
            kind=kind,
            note=note,
        )
    return None
