"""Committed records, as the checker reads them: nothing but what was written down.

The checker's input is a ledger row and the geometry that row committed to - raw
plan points with absolute UTC instants, geodetic positions, heights with their
reference, and the resource keys the plan claimed. It is *not* a planner object,
not a corridor-graph edge, not a ``geom-4d`` profile: those would make the checker
a second opinion delivered by the first one. This module is where that boundary is
enforced structurally - the record type has no field that a planner could hand
over, and an ``.importlinter`` contract stops the package importing one.

Absolute time is the checker's own contribution to the vocabulary. ``geom-core``
counts microseconds from an epoch, ``geom-4d`` interpolates on those counts, and
both are correct; this package converts a timezone-aware UTC instant to integer
microseconds with whole-number ``timedelta`` arithmetic and no ``timestamp()``
float, because "absolute time intervals" is the phrase ``technical-build/11``
section 7 uses and a float conversion is a place where an instant can drift.

Validation is a *result*, never an exception on the happy path.
:func:`validate_record` returns the reasons a record cannot be reasoned about and
the caller turns them into ``UNPROVEN``; nothing here is allowed to quietly
normalise a missing altitude reference, a naive timestamp or a repeated instant
into something that would then produce a confident ``SAFE``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from droneops_geom_core import (
    AltitudeReference,
    GeomCoreError,
    GeoPoint,
    GridSpec,
    LocalPlane,
    Millimetre,
    PlanarOffset,
    ResourceKeyError,
    kind_from_token,
    metres_to_millimetres,
    reference_from_token,
)

from .policy import CheckerPolicy
from .verdict import (
    COMMITTED_STATES,
    NON_HOLDING_STATES,
    ReasonCode,
    UnprovenReason,
)

#: Unix epoch as an aware datetime: the origin of the checker's absolute timeline.
_EPOCH: Final[datetime] = datetime(1970, 1, 1, tzinfo=UTC)

_US_PER_DAY: Final[int] = 86_400 * 1_000_000
_US_PER_SECOND: Final[int] = 1_000_000

#: Telemetry older than this cannot be used to claim the airspace is empty (I3).
#: A run overrides it; the default is the simulation's own conservative budget.
DEFAULT_TELEMETRY_BUDGET_US: Final[int] = 30 * _US_PER_SECOND

_LON_LIMIT: Final[float] = 180.0
_LAT_LIMIT: Final[float] = 90.0


def absolute_us(value: datetime) -> int | None:
    """A timezone-aware instant to integer microseconds since the Unix epoch.

    ``None`` for a naive datetime rather than an assumption: treating a bare
    ``12:00:00`` as local, as UTC, or as the operator's shift time are three
    different answers and the checker is not the component that picks one.

    Exact by construction - ``timedelta`` carries days, seconds and microseconds
    as integers, so no float appears anywhere in the conversion.
    """
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        return None
    offset = value.utcoffset() or timedelta(0)
    aware = value - offset
    delta = aware - _EPOCH
    return delta.days * _US_PER_DAY + delta.seconds * _US_PER_SECOND + delta.microseconds


def utc_instant(at_us: int) -> datetime:
    """Inverse of :func:`absolute_us`, for rendering a contact time."""
    return _EPOCH + timedelta(microseconds=at_us)


@dataclass(frozen=True)
class PlanPoint:
    """One raw plan point exactly as the committed record carries it.

    Metres and geodetic degrees, because that is what a ledger row holds and
    what a reviewer can read. Quantisation to integer millimetres happens once,
    in :func:`droneops_conflict_checker.sweep.project_track`, and nowhere else.
    """

    at: datetime
    lat_deg: float
    lon_deg: float
    altitude_m: float
    altitude_reference: str | None
    speed_mm_per_s: int = 0

    def __post_init__(self) -> None:
        if self.speed_mm_per_s < 0:
            raise ValueError(f"declared speed cannot be negative, got {self.speed_mm_per_s}")

    def is_finite(self) -> bool:
        """A NaN or infinity coordinate makes geometry meaningless, not harmless."""
        return all(
            math.isfinite(value) for value in (self.lat_deg, self.lon_deg, self.altitude_m)
        ) and -_LAT_LIMIT <= self.lat_deg <= _LAT_LIMIT and -_LON_LIMIT <= self.lon_deg <= _LON_LIMIT


@dataclass(frozen=True)
class PlannerClaim:
    """What the planner itself recorded about this pair.

    Held as data, not as a call: the checker reads the planner's *answer* out of
    the committed record, which is the only way it can be independent of how that
    answer was produced. ``allow_rule_id`` is the planner naming an exception; the
    checker has to name the same one, at the same version, from its own side.
    """

    verdict: str
    policy_version: str
    allow_rule_id: str | None = None
    allow_rule_version: str | None = None

    def __post_init__(self) -> None:
        if self.verdict not in ("SAFE", "CONFLICT", "UNPROVEN"):
            raise ValueError(f"planner verdict {self.verdict!r} is not one of the three answers")


@dataclass(frozen=True)
class CommittedPlanRecord:
    """The whole of what the checker knows about one committed plan."""

    plan_version: str
    mission_id: str
    status: str
    points: tuple[PlanPoint, ...]
    resource_keys: tuple[str, ...]
    body_radius_mm: int
    body_half_height_mm: int
    policy_release: str
    source: str = "ledger"
    restriction_expires_at: datetime | None = None
    telemetry_last_fix: datetime | None = None
    planner_claim: PlannerClaim | None = None

    @property
    def holds_airspace(self) -> bool:
        """Whether this record is part of the committed set the invariant is about."""
        return self.status.lower() in COMMITTED_STATES

    @property
    def ignored(self) -> bool:
        """A cancelled or completed record claimed no space at evaluation time."""
        return self.status.lower() in NON_HOLDING_STATES

    def corridor_identifiers(self) -> tuple[str, ...]:
        """The corridor lane ids this plan claimed, sorted, deduplicated.

        Non-corridor keys are dropped rather than rejected: a plan also claims
        volume cells, crossings and pads, and only the corridor claim is what the
        adjacent-corridor inspection compares.

        Raises:
            ResourceKeyError: a key is not canonical, which the caller reports as
                ``UNKNOWN_RESOURCE_KEY`` rather than guessing what lane was meant.
        """
        lanes: set[str] = set()
        for raw in self.resource_keys:
            kind, _, rest = raw.partition(":")
            resolved = kind_from_token(kind)
            if not rest:
                raise ResourceKeyError(f"resource key {raw!r} has an empty qualifier")
            if resolved.prefix == "corridor":
                lanes.add(rest.partition(":")[0])
        return tuple(sorted(lanes))

    def all_identifiers(self) -> tuple[str, ...]:
        """Every claimed lane, cell or facility qualifier, sorted and deduplicated."""
        found: set[str] = set()
        for raw in self.resource_keys:
            kind, _, rest = raw.partition(":")
            resolved = kind_from_token(kind)
            found.add(f"{resolved.prefix}:{rest.partition(':')[0]}")
        return tuple(sorted(found))

    def window_us(self) -> tuple[int, int] | None:
        """The record's absolute time span, or ``None`` when it cannot be established."""
        instants = [absolute_us(point.at) for point in self.points]
        if not instants or any(value is None for value in instants):
            return None
        values = [int(value) for value in instants if value is not None]
        return (min(values), max(values))


@dataclass(frozen=True)
class EvaluationContext:
    """What the checker was asked to evaluate against, and when.

    ``evaluated_at`` is supplied by the caller: a validator that read a wall clock
    would make a golden scenario's answer depend on the day it was run, and would
    make expiry checks untestable rather than merely inconvenient.
    """

    policy: CheckerPolicy
    evaluated_at: datetime
    adjacency: Mapping[str, Sequence[str]]
    telemetry_budget_us: int = DEFAULT_TELEMETRY_BUDGET_US
    grid: GridSpec | None = None

    def __post_init__(self) -> None:
        if self.telemetry_budget_us < 0:
            raise ValueError("telemetry budget cannot be negative")

    @classmethod
    def without_adjacency(cls, policy: CheckerPolicy, evaluated_at: datetime) -> EvaluationContext:
        """A context that compares only what the geometry says, with no lane map."""
        return cls(policy=policy, evaluated_at=evaluated_at, adjacency={})

    def neighbours_of(self, lane: str) -> tuple[str, ...]:
        """Declared adjacent lanes, sorted. An unknown lane has no neighbours."""
        return tuple(sorted(self.adjacency.get(lane, ())))


# ------------------------------------------------------------------------- validation
def _reason(code: ReasonCode, detail: str, plan_version: str) -> UnprovenReason:
    return UnprovenReason(code=code, detail=detail, plan_version=plan_version)


def validate_record(record: CommittedPlanRecord, context: EvaluationContext) -> tuple[UnprovenReason, ...]:
    """Everything that must be true before this record can be swept.

    Returns every reason it found rather than the first one: an operator fixing a
    record should not have to run the checker five times to discover five defects,
    and a partial list would understate how little is known about the plan.

    The order is ``code, plan_version, detail`` and the codes themselves are
    checked in the literal order of this function, so the output never depends on
    set or dict iteration.
    """
    reasons: list[UnprovenReason] = []
    label = record.plan_version or ""

    if not label.strip() or not record.resource_keys or record.planner_claim is None:
        missing = (
            "no plan version"
            if not label.strip()
            else "no recorded planner claim"
            if record.planner_claim is None
            else "no claimed resources"
        )
        reasons.append(_reason(ReasonCode.ABSENT_PROVENANCE, f"the record has {missing}", label))

    if not record.holds_airspace and not record.ignored:
        reasons.append(
            _reason(
                ReasonCode.NOT_A_COMMITTED_RECORD,
                f"state {record.status!r} is neither committed nor activated",
                label,
            )
        )

    if not record.points:
        reasons.append(_reason(ReasonCode.MALFORMED_PROFILE, "the profile has no points", label))
    if record.body_radius_mm <= 0 or record.body_half_height_mm <= 0:
        reasons.append(
            _reason(
                ReasonCode.MALFORMED_PROFILE,
                f"a body extent must be positive, got radius {record.body_radius_mm} mm and "
                f"half height {record.body_half_height_mm} mm",
                label,
            )
        )

    instants: list[int] = []
    for index, point in enumerate(record.points):
        if not point.is_finite():
            reasons.append(
                _reason(
                    ReasonCode.MALFORMED_PROFILE,
                    f"point {index} is not a finite geodetic position",
                    label,
                )
            )
            continue
        instant = absolute_us(point.at)
        if instant is None:
            reasons.append(
                _reason(
                    ReasonCode.NAIVE_ABSOLUTE_TIME,
                    f"point {index} has no UTC offset",
                    label,
                )
            )
        else:
            instants.append(instant)
        if point.altitude_reference is None:
            reasons.append(
                _reason(
                    ReasonCode.MISSING_ALTITUDE_REFERENCE,
                    f"point {index} states no altitude reference",
                    label,
                )
            )
        else:
            try:
                reference_from_token(point.altitude_reference)
            except GeomCoreError:
                reasons.append(
                    _reason(
                        ReasonCode.MISSING_ALTITUDE_REFERENCE,
                        f"point {index} names reference {point.altitude_reference!r}, which is "
                        "not one of AGL, AMSL, GROUND or PAD",
                        label,
                    )
                )
    if len(set(instants)) != len(instants):
        reasons.append(
            _reason(
                ReasonCode.NON_MONOTONIC_TIME,
                "two plan points share an instant, so interpolation has no law between them",
                label,
            )
        )
    elif any(first > second for first, second in zip(instants, instants[1:], strict=False)):
        reasons.append(
            _reason(ReasonCode.NON_MONOTONIC_TIME, "plan points are not in ascending time", label)
        )

    try:
        record.corridor_identifiers()
        record.all_identifiers()
    except ResourceKeyError as error:
        reasons.append(_reason(ReasonCode.UNKNOWN_RESOURCE_KEY, str(error), label))

    expiry = record.restriction_expires_at
    if expiry is not None:
        expired = absolute_us(expiry)
        now = absolute_us(context.evaluated_at)
        if expired is None or now is None:
            reasons.append(
                _reason(ReasonCode.NAIVE_ABSOLUTE_TIME, "an expiry stamp has no UTC offset", label)
            )
        elif expired <= now:
            reasons.append(
                _reason(
                    ReasonCode.EXPIRED_RESTRICTION_DATA,
            f"restriction data expired at {utc_instant(expired).isoformat()} and the check "
            f"runs at {utc_instant(now).isoformat()}",
                    label,
                )
            )

    fix = record.telemetry_last_fix
    if fix is not None:
        last = absolute_us(fix)
        now = absolute_us(context.evaluated_at)
        if last is None or now is None:
            reasons.append(
                _reason(ReasonCode.NAIVE_ABSOLUTE_TIME, "a telemetry stamp has no UTC offset", label)
            )
        elif now - last > context.telemetry_budget_us:
            reasons.append(
                _reason(
                    ReasonCode.STALE_TELEMETRY,
            f"last report is {(now - last) // _US_PER_SECOND} s old against a budget of "
            f"{context.telemetry_budget_us // _US_PER_SECOND} s",
                    label,
                )
            )

    claim = record.planner_claim
    if claim is not None and claim.policy_version != record.policy_release:
        reasons.append(
            _reason(
                ReasonCode.POLICY_RELEASE_MISMATCH,
                f"the plan's release {record.policy_release!r} and the planner claim's "
                f"{claim.policy_version!r} are different policy releases",
                label,
            )
        )
    if record.policy_release != context.policy.policy_version:
        reasons.append(
            _reason(
                ReasonCode.POLICY_RELEASE_MISMATCH,
                f"built under {record.policy_release!r} but the active policy is "
                f"{context.policy.policy_version!r}",
                label,
            )
        )

    return tuple(sorted(reasons, key=UnprovenReason.sort_key))


def altitude_reference_of(record: CommittedPlanRecord) -> AltitudeReference | None:
    """The single reference frame a plan is written in, or ``None`` when it has none.

    A plan that mixes ``AGL`` and ``AMSL`` points has no reference, and the answer
    is not to pick one: the datum difference between terrain height and mean sea
    level is the exact quantity a protected volume must not guess (I9).
    """
    tokens = {point.altitude_reference for point in record.points if point.altitude_reference}
    if len(tokens) != 1:
        return None
    try:
        return reference_from_token(next(iter(tokens)))
    except (GeomCoreError, StopIteration):
        return None


def point_altitude_mm(altitude_m: float) -> int:
    """Quantise a recorded height to integer millimetres, exactly once."""
    return int(metres_to_millimetres(altitude_m))


def cell_token_of(context: EvaluationContext, plane_origin: GeoPoint, east_mm: int,
                  north_mm: int, altitude_mm: int, reference: AltitudeReference) -> str | None:
    """The volume-cell token a contact falls inside, in ``geom-core``'s own vocabulary.

    The checker names the ledger row it is disputing using the same grid the ledger
    reserves, and nothing else: a conflict that cannot say which volume cell it is
    about is an argument rather than a finding. Returns ``None`` when the run did
    not freeze a grid, because inventing a cell size would be inventing a key.

    Raises:
        GeomCoreError: the point is outside the projection or the grid domain, in
            which case the caller reports ``PLANAR_EXTENT_EXCEEDED``.
    """
    if context.grid is None:
        return None
    plane = LocalPlane(plane_origin)
    offset = PlanarOffset(east_mm=Millimetre(east_mm), north_mm=Millimetre(north_mm))
    cell = plane.volume_cell_of(
        context.grid,
        plane.point_at(offset),
        altitude_mm,
        level=min(context.grid.max_level, 5),
        reference=reference,
    )
    return cell.token()
