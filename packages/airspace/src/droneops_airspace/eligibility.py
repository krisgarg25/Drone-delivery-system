"""Route eligibility: does this path, at these times and heights, meet a restriction?

Two input shapes are accepted because two consumers exist: a plan's route profile
(``planner``, with a time-indexed waypoint list) and a corridor edge list
(``corridor-graph``, with an occupancy window per edge). Both are reduced to the same
internal :class:`_Occupancy` - a path, a vertical band, a UTC interval - so the answer
cannot differ between "the route" and "the edges it was built from".

What a result can say:

``allowed_by_model``
    Nothing in the snapshot intersects this occupancy. Scoped exactly: it is a statement
    about the evaluated data at the evaluation instant, and the snapshot's own assessment
    must have been usable for the result to be trusted at all.
``allowed_with_condition``
    A conditional restriction (yellow, notified, airport vicinity, facility outage) is
    crossed. The named condition must be recorded before launch; silence is never read as
    compliance.
``blocked_now``
    A prohibiting restriction is in force, fresh, and overlaps this occupancy.
``blocked_during_window``
    The overlap exists but the restriction is not yet in force at the decision instant:
    the plan is not blocked *now*, it is blocked during the stated window, which is the
    difference between "reroute" and "reschedule".
``no_launch_insufficient_data``
    A restriction that could affect this occupancy cannot be evaluated: stale, expired,
    source-less, clock-skewed, mixed altitude references, or a snapshot nobody sourced.
    This is a refusal to decide, and it is a no-launch.

``no_launch_insufficient_data`` outranks everything else, because a decision made on
unusable data is not a conservative decision, it is an unexplained one.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import NoReturn

from pydantic import Field, model_validator

from droneops_contracts.geometry import AltitudeReference
from droneops_contracts.provenance import ContractModel, UtcDatetime, ensure_utc
from droneops_geom_core import AltitudeBand, GeoPoint
from droneops_geom_core.altitude import AltitudeReference as KernelReference
from droneops_geom_core.altitude import AltitudeReferenceError

from .errors import AirspaceError, GeometryInputError, refuse_clearance
from .geometry import Footprint, comparable_footprint
from .restriction import NOT_A_CLEARANCE, Restriction, RestrictionEffect
from .snapshot import RestrictionSnapshot, SnapshotAssessment
from .validity import FreshnessPolicy, RestrictionValidity, evaluate_restriction

MIN_THICKNESS_MM = 1
"""A point occupancy is widened by one millimetre: a half-open band must not miss it."""

FAR_FUTURE = datetime(2100, 1, 1, tzinfo=UTC)


class EligibilityVerdict(StrEnum):
    ALLOWED_BY_MODEL = "allowed_by_model"
    ALLOWED_WITH_CONDITION = "allowed_with_condition"
    BLOCKED_NOW = "blocked_now"
    BLOCKED_DURING_WINDOW = "blocked_during_window"
    NO_LAUNCH_INSUFFICIENT_DATA = "no_launch_insufficient_data"
    NOT_EVALUATED = "not_evaluated"


VERDICT_PRECEDENCE: tuple[EligibilityVerdict, ...] = (
    EligibilityVerdict.NO_LAUNCH_INSUFFICIENT_DATA,
    EligibilityVerdict.BLOCKED_NOW,
    EligibilityVerdict.BLOCKED_DURING_WINDOW,
    EligibilityVerdict.ALLOWED_WITH_CONDITION,
    EligibilityVerdict.ALLOWED_BY_MODEL,
    EligibilityVerdict.NOT_EVALUATED,
)
"""Worst-first. The most refusing verdict found anywhere wins the aggregate."""

NON_LAUNCHABLE: frozenset[EligibilityVerdict] = frozenset(
    {
        EligibilityVerdict.BLOCKED_NOW,
        EligibilityVerdict.BLOCKED_DURING_WINDOW,
        EligibilityVerdict.NO_LAUNCH_INSUFFICIENT_DATA,
        EligibilityVerdict.NOT_EVALUATED,
    }
)


class RouteWaypoint(ContractModel):
    """Geodetic point, explicit altitude with reference, seconds from plan start."""

    lat: float = Field(ge=-90.0, le=90.0)
    lon: float = Field(ge=-180.0, le=180.0)
    altitude_m: float
    altitude_reference: AltitudeReference
    t_s: int = Field(ge=0)

    @model_validator(mode="after")
    def _check(self) -> RouteWaypoint:
        if self.altitude_reference is AltitudeReference.UNSPECIFIED:
            raise ValueError("a route waypoint without an altitude reference is unusable (I9)")
        return self

    @property
    def point(self) -> GeoPoint:
        return GeoPoint(self.lat, self.lon)


class RouteProfile(ContractModel):
    """A planned path: waypoints on one clock, anchored at ``planned_start_utc``."""

    plan_id: str = Field(min_length=1)
    planned_start_utc: UtcDatetime
    waypoints: tuple[RouteWaypoint, ...] = Field(min_length=2)

    @property
    def window(self) -> tuple[datetime, datetime]:
        end = self.planned_start_utc + timedelta(seconds=self.waypoints[-1].t_s)
        return (self.planned_start_utc, end)


class CorridorEdgeProfile(ContractModel):
    """One graph edge with the window it would be occupied in, relative to a horizon."""

    edge_id: str = Field(min_length=1)
    waypoints: tuple[RouteWaypoint, ...] = Field(min_length=2)
    entry_t_s: int = Field(ge=0)
    exit_t_s: int = Field(gt=0)

    @model_validator(mode="after")
    def _check(self) -> CorridorEdgeProfile:
        if self.exit_t_s <= self.entry_t_s:
            raise ValueError("edge exit time must be after its entry time")
        return self


class IntersectionReport(ContractModel):
    """One restriction against one occupancy: what met what, when, and why."""

    restriction_id: str
    kind: str
    effect: str
    subject: str
    verdict: EligibilityVerdict
    waypoint_hits: tuple[int, ...] = ()
    contains_endpoints: tuple[bool, bool] = (False, False)
    altitude_overlap: bool = False
    blocked_from: UtcDatetime | None = None
    blocked_until: UtcDatetime | None = None
    condition: str | None = None
    condition_satisfied: bool = False
    trust: str
    window: str
    reasons: tuple[str, ...] = ()
    note: str = NOT_A_CLEARANCE

    @property
    def intersects(self) -> bool:
        return self.verdict is not EligibilityVerdict.ALLOWED_BY_MODEL

    @property
    def blocked_window(self) -> tuple[datetime, datetime] | None:
        if self.blocked_from is None or self.blocked_until is None:
            return None
        return (self.blocked_from, self.blocked_until)


class EligibilityResult(ContractModel):
    """The answer for one subject: a route, an edge set, or a single plan."""

    subject_id: str
    verdict: EligibilityVerdict
    launch_permitted: bool
    reports: tuple[IntersectionReport, ...] = ()
    outstanding_conditions: tuple[str, ...] = ()
    snapshot: SnapshotAssessment | None = None
    authoritative: bool = False
    note: str = NOT_A_CLEARANCE

    @property
    def intersecting(self) -> tuple[IntersectionReport, ...]:
        return tuple(report for report in self.reports if report.intersects)

    def as_flight_authorisation(self) -> NoReturn:
        """The interpretation this type refuses. There is no other meaning of a verdict."""

        refuse_clearance(self.subject_id)


def _ref(reference: AltitudeReference) -> KernelReference:
    return KernelReference(reference.value)


def _band_of(waypoints: Sequence[RouteWaypoint]) -> AltitudeBand:
    """The vertical extent of a path, refusing to mix references (I9)."""

    references = {_ref(point.altitude_reference) for point in waypoints}
    if len(references) > 1:
        raise AltitudeReferenceError(
            f"waypoints mix altitude references {sorted(r.value for r in references)}; "
            "no datum was supplied, so the vertical extent is unknown rather than wide"
        )
    reference = references.pop()
    floor_m = min(point.altitude_m for point in waypoints)
    top_m = max(point.altitude_m for point in waypoints)
    upper = top_m + MIN_THICKNESS_MM / 1000.0 if top_m <= floor_m else top_m
    return AltitudeBand.from_metres(floor_m, upper, reference)


@dataclass(frozen=True)
class _Occupancy:
    label: str
    path: tuple[GeoPoint, ...]
    band: AltitudeBand
    start: datetime
    end: datetime


@dataclass(frozen=True)
class _Subject:
    subject_id: str
    occupancies: tuple[_Occupancy, ...]


def _route_subject(profile: RouteProfile) -> _Subject:
    occupancies: list[_Occupancy] = []
    for index in range(len(profile.waypoints) - 1):
        first = profile.waypoints[index]
        second = profile.waypoints[index + 1]
        occupancies.append(
            _Occupancy(
                label=f"segment {index} ({first.t_s}s->{second.t_s}s)",
                path=(first.point, second.point),
                band=_band_of((first, second)),
                start=profile.planned_start_utc + timedelta(seconds=first.t_s),
                end=profile.planned_start_utc + timedelta(seconds=second.t_s),
            )
        )
    return _Subject(profile.plan_id, tuple(occupancies))


def _edge_subject(
    edges: Sequence[CorridorEdgeProfile], *, window_start_utc: UtcDatetime
) -> _Subject:
    occupancies = tuple(
        _Occupancy(
            label=edge.edge_id,
            path=tuple(point.point for point in edge.waypoints),
            band=_band_of(edge.waypoints),
            start=window_start_utc + timedelta(seconds=edge.entry_t_s),
            end=window_start_utc + timedelta(seconds=edge.exit_t_s),
        )
        for edge in edges
    )
    return _Subject("corridor-edges", occupancies)


def _time_overlap(
    restriction: Restriction, occupancy: _Occupancy
) -> tuple[datetime, datetime] | None:
    """Intersection of the claim's window with the occupancy interval, if any."""

    if restriction.effective_until is not None and restriction.effective_until <= occupancy.start:
        return None
    lower = max(restriction.effective_from, occupancy.start)
    upper = min(restriction.effective_until or FAR_FUTURE, occupancy.end)
    if upper <= lower:
        return None
    return (lower, upper)


def _geometry_hits(footprint: Footprint, occupancy: _Occupancy) -> tuple[tuple[int, ...], bool]:
    """``(segment indices, any endpoint inside)`` - raising nothing, guessing nothing."""

    if footprint.separated_from(occupancy.path):
        return (), False
    comparable = comparable_footprint(footprint, occupancy.path)
    hits = comparable.crossed_segments(occupancy.path)
    inside = tuple(comparable.contains(point) for point in occupancy.path)
    return hits, any(inside)


def _altitude_overlap(restriction: Restriction, occupancy: _Occupancy) -> bool:
    return restriction.band.intersects(occupancy.band)


def _fail_closed_report(
    restriction: Restriction,
    validity: RestrictionValidity,
    subject: str,
    reasons: Sequence[str],
) -> IntersectionReport:
    return IntersectionReport(
        restriction_id=restriction.restriction_id,
        kind=restriction.kind.value,
        effect=restriction.effect.value,
        subject=subject,
        verdict=EligibilityVerdict.NO_LAUNCH_INSUFFICIENT_DATA,
        trust=validity.trust.value,
        window=validity.window.value,
        reasons=(
            *reasons,
            "a restriction that cannot be evaluated is a no-launch, not a no-restriction",
        ),
    )


def _altitude_may_overlap(restriction: Restriction, occupancy: _Occupancy) -> bool | None:
    """``True`` / ``False``, or ``None`` when the two bands cannot be compared at all.

    ``None`` is the answer that matters: mixed altitude references with no datum supplied
    leave the vertical relationship unknown, and unknown is not "does not overlap".
    """

    try:
        return _altitude_overlap(restriction, occupancy)
    except AltitudeReferenceError:
        return None


def _assess_pair(
    restriction: Restriction,
    occupancy: _Occupancy,
    validity: RestrictionValidity,
    *,
    conditions_satisfied: frozenset[str],
) -> IntersectionReport | None:
    """One restriction against one occupancy, or ``None`` when it provably misses."""

    try:
        hits, endpoints_inside = _geometry_hits(restriction.geometry, occupancy)
    except GeometryInputError as error:
        # No shared plane: the claim cannot be ruled out, so it cannot be ruled in either.
        return _fail_closed_report(
            restriction,
            validity,
            occupancy.label,
            (f"footprint and path are unprojectable: {error}",),
        )
    if hits == () and not endpoints_inside:
        return None
    if not validity.data_is_usable:
        return _fail_closed_report(restriction, validity, occupancy.label, validity.reasons)
    overlap = _time_overlap(restriction, occupancy)
    altitude = _altitude_may_overlap(restriction, occupancy)
    if overlap is None or altitude is False:
        return None
    if altitude is None:
        return _fail_closed_report(
            restriction,
            validity,
            occupancy.label,
            ("band comparison requires one altitude reference and none was supplied (I9)",),
        )
    return _overlap_report(
        restriction,
        occupancy,
        validity,
        overlap,
        hits=hits,
        endpoints_inside=endpoints_inside,
        conditions_satisfied=conditions_satisfied,
    )


def _overlap_report(
    restriction: Restriction,
    occupancy: _Occupancy,
    validity: RestrictionValidity,
    overlap: tuple[datetime, datetime],
    *,
    hits: tuple[int, ...],
    endpoints_inside: bool,
    conditions_satisfied: frozenset[str],
) -> IntersectionReport:
    condition = restriction.condition
    satisfied = condition is None or condition in conditions_satisfied
    verdict = _verdict_for(restriction, validity, occupancy, overlap)
    reasons = list(validity.reasons)
    if verdict is EligibilityVerdict.BLOCKED_NOW:
        reasons.append("in force at the decision instant and covering the start of this occupancy")
    if verdict is EligibilityVerdict.BLOCKED_DURING_WINDOW:
        reasons.append(
            f"not blocked at the decision instant; blocked from {overlap[0].isoformat()} to "
            f"{overlap[1].isoformat()}, which is a reschedule question, not a reroute one"
        )
    if verdict is EligibilityVerdict.ALLOWED_WITH_CONDITION and not satisfied:
        reasons.append(
            f"condition {condition!r} is not recorded: silence is never read as compliance here"
        )
    return IntersectionReport(
        restriction_id=restriction.restriction_id,
        kind=restriction.kind.value,
        effect=restriction.effect.value,
        subject=occupancy.label,
        verdict=verdict,
        waypoint_hits=hits,
        contains_endpoints=(endpoints_inside, endpoints_inside),
        altitude_overlap=True,
        blocked_from=overlap[0],
        blocked_until=overlap[1],
        condition=condition,
        condition_satisfied=satisfied,
        trust=validity.trust.value,
        window=validity.window.value,
        reasons=tuple(reasons),
    )


def _verdict_for(
    restriction: Restriction,
    validity: RestrictionValidity,
    occupancy: _Occupancy,
    overlap: tuple[datetime, datetime],
) -> EligibilityVerdict:
    """Blocked now, blocked during a window, or allowed with a condition.

    ``blocked_now`` means the claim is in force at the decision instant *and* already
    covers the first instant of this occupancy: there is no way to begin it. Anything
    else that overlaps carries the interval so a planner can move around it.
    """

    if restriction.effect is not RestrictionEffect.PROHIBIT:
        return EligibilityVerdict.ALLOWED_WITH_CONDITION
    in_force_now = validity.applies
    covers_start = overlap[0] <= occupancy.start
    if in_force_now and covers_start:
        return EligibilityVerdict.BLOCKED_NOW
    return EligibilityVerdict.BLOCKED_DURING_WINDOW


def _worst(verdicts: Sequence[EligibilityVerdict]) -> EligibilityVerdict:
    found = set(verdicts)
    for verdict in VERDICT_PRECEDENCE:
        if verdict in found:
            return verdict
    return EligibilityVerdict.ALLOWED_BY_MODEL


def _evaluate(
    subject: _Subject,
    snapshot: RestrictionSnapshot,
    *,
    now: UtcDatetime,
    policy: FreshnessPolicy,
    conditions_satisfied: frozenset[str],
) -> EligibilityResult:
    ensure_utc(now)
    assessment = snapshot.assess(now, policy)
    reports: list[IntersectionReport] = []
    for restriction in snapshot.restrictions:
        validity = evaluate_restriction(restriction, now, policy)
        for occupancy in subject.occupancies:
            report = _assess_pair(
                restriction, occupancy, validity, conditions_satisfied=conditions_satisfied
            )
            if report is not None:
                reports.append(report)
    if not assessment.data_usable:
        verdict = EligibilityVerdict.NO_LAUNCH_INSUFFICIENT_DATA
    else:
        verdict = _worst([report.verdict for report in reports])
    outstanding = tuple(
        sorted(
            {
                report.condition
                for report in reports
                if report.condition and not report.condition_satisfied
            }
        )
    )
    launch_permitted = verdict not in NON_LAUNCHABLE and not outstanding
    return EligibilityResult(
        subject_id=subject.subject_id,
        verdict=verdict,
        launch_permitted=launch_permitted,
        reports=tuple(reports),
        outstanding_conditions=outstanding,
        snapshot=assessment,
        note=NOT_A_CLEARANCE,
    )


def evaluate_route(
    profile: RouteProfile,
    snapshot: RestrictionSnapshot,
    *,
    now: UtcDatetime,
    policy: FreshnessPolicy | None = None,
    conditions_satisfied: frozenset[str] = frozenset(),
) -> EligibilityResult:
    """Eligibility of a planned route as scheduled. Not a clearance: see ``note``."""

    return _evaluate(
        _route_subject(profile),
        snapshot,
        now=now,
        policy=policy or FreshnessPolicy(),
        conditions_satisfied=conditions_satisfied,
    )


def evaluate_corridor_edges(
    edges: Sequence[CorridorEdgeProfile],
    snapshot: RestrictionSnapshot,
    *,
    window_start_utc: UtcDatetime,
    now: UtcDatetime,
    policy: FreshnessPolicy | None = None,
    conditions_satisfied: frozenset[str] = frozenset(),
) -> CorridorEligibility:
    """Eligibility of a candidate edge set within one occupancy window."""

    if not edges:
        raise AirspaceError("an empty edge set is not an answer: it is an unanswered question")
    subject = _edge_subject(edges, window_start_utc=window_start_utc)
    result = _evaluate(
        subject,
        snapshot,
        now=now,
        policy=policy or FreshnessPolicy(),
        conditions_satisfied=conditions_satisfied,
    )
    blocked = {report.subject for report in result.reports if report.verdict in NON_LAUNCHABLE}
    conditional = {report.subject for report in result.reports if not report.condition_satisfied}
    usable = result.snapshot is not None and result.snapshot.data_usable
    return CorridorEligibility(
        edge_ids=tuple(edge.edge_id for edge in edges),
        overall=result,
        usable_data=usable,
        blocked_edge_ids=tuple(sorted(blocked)),
        condition_edge_ids=tuple(sorted(conditional - blocked)),
        note=NOT_A_CLEARANCE,
    )


class CorridorEligibility(ContractModel):
    """Per-edge view of one evaluation: which edges a planner may keep proposing."""

    edge_ids: tuple[str, ...]
    overall: EligibilityResult
    usable_data: bool
    blocked_edge_ids: tuple[str, ...] = ()
    condition_edge_ids: tuple[str, ...] = ()
    note: str = NOT_A_CLEARANCE

    @property
    def eligible_edge_ids(self) -> tuple[str, ...]:
        if not self.usable_data:
            return ()
        refused = set(self.blocked_edge_ids)
        return tuple(edge for edge in self.edge_ids if edge not in refused)

    def as_flight_authorisation(self) -> NoReturn:
        refuse_clearance(f"corridor eligibility {self.overall.subject_id}")


def evaluate_plan_eligibility(
    profile: RouteProfile,
    snapshot: RestrictionSnapshot,
    *,
    now: UtcDatetime,
    policy: FreshnessPolicy | None = None,
    conditions_satisfied: frozenset[str] = frozenset(),
) -> EligibilityResult:
    """Alias of :func:`evaluate_route` named for the plan-invalidations it feeds."""

    return evaluate_route(
        profile,
        snapshot,
        now=now,
        policy=policy,
        conditions_satisfied=conditions_satisfied,
    )
