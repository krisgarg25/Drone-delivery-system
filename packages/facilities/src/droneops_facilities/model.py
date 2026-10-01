"""The facility domain model: capacity, the turnaround chain, claims and results.

Three ideas carry this whole package.

**Capacity is a set of unit resources, not a number to decrement.** A profile
with four pads owns four single-occupancy pads, four charger bays own four
single-occupancy bays, and so on for crew. Overbooking would require two claims
to share one identity, which the scheduler's first-fit search cannot produce and
which :meth:`CommitLedger.commit` rejects at the boundary. This is invariant I6
by construction rather than by accounting.

**The turnaround is five timed stages on a pad plus a sixth on a charger.**
Approach, landing, handover/transfer, loading and departure are contiguous
inside one pad claim, each with its own window so each is separately measurable;
recharge is a separate claim on a separate resource. Crew is a third resource
whose window must cover handover plus loading. That separation is what makes the
realistic failures expressible: "pad free but no crew" and "crew free but
charger occupied" are different reasons with different codes.

**Facilities propose; the ledger commits.** Nothing here mutates a committed
claim. An outage blocks *new* claims and preserves *existing* ones - see
:class:`OutageImpact`, whose constructor refuses to build a "drop the committed
work" answer.

All times are integer microseconds since the geom-core epoch (``conventions.md``
section 4): no wall clock, no floats, no locale.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from typing import Final

from droneops_contracts.facilities import FacilityStatus, HandoverType
from droneops_geom_core import (
    MICROS_PER_MINUTE,
    Interval,
    ResourceKey,
    ResourceKind,
    as_duration_us,
    checked_add,
)

from .errors import CapacityError, HandoverTypeError, OutageError
from .resources import UnitResource, charger_resources, crew_resources, pad_resources

# The Indian government medical delivery template mandates land-and-transfer;
# WINCH and PARACHUTE are representable in the schema for foreign references and
# rejected here, exactly as droneops_contracts.Pad does.
SUPPORTED_HANDOVER: Final[frozenset[HandoverType]] = frozenset({HandoverType.LAND_TRANSFER})


def require_supported_handover(value: HandoverType) -> HandoverType:
    """Return ``value`` or raise. There is no default handover type.

    Raises:
        HandoverTypeError: a winch or parachute transfer was requested.
    """
    if value not in SUPPORTED_HANDOVER:
        raise HandoverTypeError(
            f"handover type {value.value!r} is rejected: this build is land-transfer only"
        )
    return value


class ClaimPurpose(str, Enum):
    """Which physical facility resource a claim sits on."""

    PAD = "pad"
    CHARGER = "charger"
    CREW = "crew"


PURPOSE_BY_KIND: Final[dict[ResourceKind, ClaimPurpose]] = {
    ResourceKind.PAD: ClaimPurpose.PAD,
    ResourceKind.CHARGER_BAY: ClaimPurpose.CHARGER,
    ResourceKind.CREW: ClaimPurpose.CREW,
}


def purpose_for_kind(kind: ResourceKind) -> ClaimPurpose:
    """The facility purpose of a canonical resource kind.

    Raises:
        KeyError: an airspace kind reached a facility claim. There is no
            default, because a pad claim silently becoming a volume claim would
            be an I6 hole.
    """
    return PURPOSE_BY_KIND[kind]


class TurnaroundStage(str, Enum):
    """One timed link of the turnaround chain (``technical-build/01`` section 11)."""

    APPROACH = "approach"
    LANDING = "landing"
    HANDOVER = "handover"
    LOADING = "loading"
    DEPARTURE = "departure"
    RECHARGE = "recharge"

    @property
    def resource(self) -> ClaimPurpose:
        """The resource this stage occupies."""
        return ClaimPurpose.CHARGER if self is TurnaroundStage.RECHARGE else ClaimPurpose.PAD


TURNAROUND_CHAIN: Final[tuple[TurnaroundStage, ...]] = (
    TurnaroundStage.APPROACH,
    TurnaroundStage.LANDING,
    TurnaroundStage.HANDOVER,
    TurnaroundStage.LOADING,
    TurnaroundStage.DEPARTURE,
)
"""The pad-side chain, in the order it happens. ``RECHARGE`` is deliberately
absent: it is a charger claim and must not be folded into pad occupancy."""


class WaitReason(str, Enum):
    """Why work is not running right now.

    Phase 1's report and Phase 4's console show these as distinct queue causes,
    so ``PAD_WAIT`` and ``CHARGER_WAIT`` must never collapse into one bucket.
    """

    NONE = "none"
    PAD_WAIT = "pad_wait"
    CHARGER_WAIT = "charger_wait"
    CREW_UNAVAILABLE = "crew_unavailable"
    OUTSIDE_OPENING_HOURS = "outside_opening_hours"
    FACILITY_OUTAGE = "facility_outage"
    POLICY_VERSION_MISMATCH = "policy_version_mismatch"
    HANDOVER_TYPE_UNSUPPORTED = "handover_type_unsupported"
    NO_SUCH_FACILITY = "no_such_facility"
    HORIZON_EXCEEDED = "horizon_exceeded"
    PAD_TOO_SMALL = "pad_too_small"


@dataclass(frozen=True)
class TurnaroundDurations:
    """Per-stage durations for one turnaround, integer microseconds.

    ``loading_us`` and ``handover_us`` come from the scenario's declared
    ``loading_turnaround_min`` / ``offload_turnaround_min``; the flight-deck
    stages are illustrative experiment parameters and are stated in the profile.
    """

    approach_us: int
    landing_us: int
    handover_us: int
    loading_us: int
    departure_us: int

    def __post_init__(self) -> None:
        for name in ("approach_us", "landing_us", "handover_us", "loading_us", "departure_us"):
            as_duration_us(getattr(self, name))
        if self.total_us == 0:
            raise CapacityError("a turnaround of zero length schedules no pad time")

    @property
    def total_us(self) -> int:
        """Pad time one aircraft occupies: the whole chain."""
        return (
            self.approach_us + self.landing_us + self.handover_us + self.loading_us + self.departure_us
        )

    @property
    def pre_crew_us(self) -> int:
        """Pad time before crew is needed: approach plus landing."""
        return checked_add(self.approach_us, self.landing_us)

    @property
    def crew_us(self) -> int:
        """The crewed span: handover plus loading."""
        return checked_add(self.handover_us, self.loading_us)

    def offsets(self) -> tuple[tuple[TurnaroundStage, Interval], ...]:
        """Each stage as a zero-anchored interval - the documented chain order.

        Offsets, not absolute windows: the scheduler adds the granted pad start,
        so the chain can be re-planned without recomputing durations.
        """
        cursor = 0
        spans: list[tuple[TurnaroundStage, Interval]] = []
        for stage, duration in (
            (TurnaroundStage.APPROACH, self.approach_us),
            (TurnaroundStage.LANDING, self.landing_us),
            (TurnaroundStage.HANDOVER, self.handover_us),
            (TurnaroundStage.LOADING, self.loading_us),
            (TurnaroundStage.DEPARTURE, self.departure_us),
        ):
            spans.append((stage, Interval.of(cursor, checked_add(cursor, duration))))
            cursor = checked_add(cursor, duration)
        return tuple(spans)

    @classmethod
    def from_minutes(
        cls,
        *,
        loading_min: int,
        offload_min: int,
        approach_min: int = 0,
        landing_min: int = 0,
        departure_min: int = 0,
    ) -> TurnaroundDurations:
        """Build durations from the whole-minute values the bundle declares."""
        return cls(
            approach_us=approach_min * MICROS_PER_MINUTE,
            landing_us=landing_min * MICROS_PER_MINUTE,
            handover_us=offload_min * MICROS_PER_MINUTE,
            loading_us=loading_min * MICROS_PER_MINUTE,
            departure_us=departure_min * MICROS_PER_MINUTE,
        )


@dataclass(frozen=True)
class OutageWindow:
    """A declared closure of a facility over ``[start_us, end_us)``."""

    start_us: int
    end_us: int
    state: FacilityStatus = FacilityStatus.OUTAGE
    reason: str = "declared outage"

    def __post_init__(self) -> None:
        if self.end_us < self.start_us:
            raise OutageError(f"outage {self.start_us}..{self.end_us} is reversed")

    @property
    def window(self) -> Interval:
        return Interval.of(self.start_us, self.end_us)

    def overlaps(self, other: Interval) -> bool:
        return self.window.overlaps(other)


@dataclass(frozen=True)
class Accessibility:
    """How a parcel physically changes hands at a site."""

    handover_type: HandoverType
    step_free: bool
    ground_pickup: bool
    requires_crew: bool

    def __post_init__(self) -> None:
        require_supported_handover(self.handover_type)


class OutageState(str, Enum):
    OPEN = "open"
    OUTAGE = "outage"
    CLOSED = "closed"
    PARTIAL = "partial"


@dataclass(frozen=True)
class OutageImpact:
    """The asymmetry an outage must produce, made explicit in the API.

    ``blocks_new_claims`` may be true while committed work is retained; the
    reverse is a defect, so the constructor rejects any impact that would drop
    or cancel a committed claim. Reconciliation is a *later* superseding record
    (``conventions.md`` section 5), never an in-place edit made here.
    """

    facility_id: str
    state: OutageState
    blocks_new_claims: bool
    requires_reconciliation: bool
    affected_claims: tuple[ResourceClaim, ...] = ()
    outage: OutageWindow | None = None

    @property
    def committed_claims_preserved(self) -> bool:
        """Always true, and deliberately not a constructor argument.

        There is no code path by which a facility scheduler reports an outage
        impact that drops committed work: reconciliation is a superseding record
        raised by the ledger, so the only honest answer here is ``True``.
        """
        return True

    @classmethod
    def unaffected(cls, facility_id: str) -> OutageImpact:
        return cls(facility_id, OutageState.OPEN, blocks_new_claims=False, requires_reconciliation=False)

    def as_metrics(self) -> dict[str, str | int | bool]:
        """The report shape for Phase 1's outage column, deterministic."""
        return {
            "facility_id": self.facility_id,
            "outage_state": self.state.value,
            "blocks_new_claims": self.blocks_new_claims,
            "committed_claims_preserved": self.committed_claims_preserved,
            "requires_reconciliation": self.requires_reconciliation,
            "committed_claims_affected": len(self.affected_claims),
        }


@dataclass(frozen=True)
class CapacityProfile:
    """Everything section 11 requires a site to expose, in one frozen record."""

    facility_id: str
    kind: str
    name: str
    status: FacilityStatus
    opens_us: int
    closes_us: int
    pad_count: int
    charger_bays: int
    crew_slots: int
    turnaround: TurnaroundDurations
    accessibility: Accessibility
    charge_power_w: int
    crew_shifts: tuple[Interval, ...] = ()
    outages: tuple[OutageWindow, ...] = ()
    policy_version: str = "sim-facility-policy-v1"
    authoritative: bool = False
    source: str = "synthetic"

    def __post_init__(self) -> None:
        if not self.facility_id:
            raise CapacityError("facility_id must not be empty")
        if self.closes_us < self.opens_us:
            raise CapacityError(f"facility {self.facility_id} closes before it opens")
        if self.pad_count < 0 or self.charger_bays < 0 or self.crew_slots < 0:
            raise CapacityError(f"facility {self.facility_id} declares negative capacity")
        if self.charge_power_w < 0:
            raise CapacityError(f"facility {self.facility_id} declares negative charge power")
        require_supported_handover(self.accessibility.handover_type)

    @property
    def open_window(self) -> Interval:
        return Interval.of(self.opens_us, self.closes_us)

    @property
    def loading_us(self) -> int:
        return self.turnaround.loading_us

    @property
    def offload_us(self) -> int:
        return self.turnaround.handover_us

    @property
    def turnaround_us(self) -> int:
        return self.turnaround.total_us

    @property
    def shift_windows(self) -> tuple[Interval, ...]:
        """Crew duty windows, ascending and merged."""
        return tuple(sorted(self.crew_shifts, key=lambda item: (item.start, item.end)))

    def pads(self) -> tuple[UnitResource, ...]:
        return pad_resources(self.facility_id, self.pad_count)

    def chargers(self) -> tuple[UnitResource, ...]:
        return charger_resources(self.facility_id, self.charger_bays)

    def crews(self) -> tuple[UnitResource, ...]:
        return crew_resources(self.facility_id, self.crew_slots)

    def durations_for(self, override: TurnaroundDurations | None) -> TurnaroundDurations:
        """The request's own durations if it brought them, else the site default."""
        return override if override is not None else self.turnaround

    def usable_windows(self, horizon: Interval) -> tuple[Interval, ...]:
        """Windows in which *new* claims may start: open, in horizon, outage-free.

        An outage subtracts from this set, which is how "accepts nothing new" is
        enforced without a special case in the scheduler.
        """
        base = self.open_window.intersection(horizon)
        if not base:
            return ()
        available = base[0]
        free: list[Interval] = [available]
        for outage in self.outages:
            free = [piece for held in free for piece in held.difference(outage.window)]
        return tuple(sorted(free, key=lambda item: (item.start, item.end)))

    def crew_windows(self, horizon: Interval) -> tuple[Interval, ...]:
        """Windows in which crewed stages (handover, loading) may run.

        Crew shifts are intersected with opening times and then reduced by
        outages: a hub that is closed has nobody handling parcels in it.
        """
        if not self.crew_shifts:
            return self.usable_windows(horizon)
        usable = self.usable_windows(horizon)
        out: list[Interval] = []
        for shift in self.crew_shifts:
            for window in usable:
                out.extend(shift.intersection(window))
        return tuple(sorted(out, key=lambda item: (item.start, item.end)))

    def outage_covering(self, window: Interval) -> OutageWindow | None:
        """The first outage overlapping ``window``, in ascending order."""
        for outage in sorted(self.outages, key=lambda item: (item.start_us, item.end_us)):
            if outage.overlaps(window):
                return outage
        return None

    def outage_state_at(self, at_us: int) -> OutageState:
        if self.status is FacilityStatus.OUTAGE:
            return OutageState.OUTAGE
        if self.status is FacilityStatus.CLOSED:
            return OutageState.CLOSED
        for outage in self.outages:
            if outage.window.contains_instant(at_us):
                return OutageState.OUTAGE
        return OutageState.OPEN

    def impact_of_outage(self, at_us: int, committed: Iterable[ResourceClaim]) -> OutageImpact:
        """What an outage at ``at_us`` means for this site - explicitly asymmetric.

        New claims are blocked; the committed claims the outage overlaps come
        back as ``affected_claims`` with ``requires_reconciliation`` set, because
        losing them silently would be a worse failure than the outage itself.
        """
        state = self.outage_state_at(at_us)
        if state is OutageState.OPEN:
            return OutageImpact.unaffected(self.facility_id)
        claims = tuple(
            claim
            for claim in sorted(committed, key=lambda item: item.identity_token)
            if claim.facility_id == self.facility_id
        )
        affected = tuple(claim for claim in claims if claim.window.start <= at_us < claim.window.end)
        return OutageImpact(
            facility_id=self.facility_id,
            state=state,
            blocks_new_claims=True,
            requires_reconciliation=bool(affected),
            affected_claims=affected,
            outage=self.outage_covering(Interval.of(at_us, at_us + 1)),
        )

    def capacity_view(self, horizon: Interval | None = None) -> FacilityCapacityView:
        """The exposed capacity snapshot for reports and the console."""
        window = horizon if horizon is not None else self.open_window
        return FacilityCapacityView(
            facility_id=self.facility_id,
            kind=self.kind,
            name=self.name,
            opening_times=window,
            simultaneous_pads=self.pad_count,
            charger_bays=self.charger_bays,
            crew_slots=self.crew_slots,
            turnaround_us=self.turnaround.total_us,
            loading_us=self.turnaround.loading_us,
            offload_us=self.turnaround.handover_us,
            accessibility=self.accessibility,
            outage_state=self.outage_state_at(window.start),
            usable_windows=self.usable_windows(window),
            policy_version=self.policy_version,
            authoritative=self.authoritative,
        )


@dataclass(frozen=True)
class FacilityCapacityView:
    """Exactly the section 11 capacity list, as data one read-only call returns.

    A hub may have capacity here and still be unusable: ``simultaneous_pads``
    says nothing about crew, and ``crew_slots`` says nothing about chargers. The
    three counts are deliberately separate fields for that reason.
    """

    facility_id: str
    kind: str
    name: str
    opening_times: Interval
    simultaneous_pads: int
    charger_bays: int
    crew_slots: int
    turnaround_us: int
    loading_us: int
    offload_us: int
    accessibility: Accessibility
    outage_state: OutageState
    usable_windows: tuple[Interval, ...]
    policy_version: str
    authoritative: bool

    def as_metrics(self) -> dict[str, str | int]:
        """A flat, key-sorted metric record for the report and console."""
        flat: dict[str, str | int] = {
            "facility_id": self.facility_id,
            "kind": self.kind,
            "opening_start_us": self.opening_times.start,
            "opening_end_us": self.opening_times.end,
            "simultaneous_pads": self.simultaneous_pads,
            "charger_bays": self.charger_bays,
            "crew_slots": self.crew_slots,
            "turnaround_us": self.turnaround_us,
            "loading_us": self.loading_us,
            "offload_us": self.offload_us,
            "handover_type": self.accessibility.handover_type.value,
            "step_free": int(self.accessibility.step_free),
            "ground_pickup": int(self.accessibility.ground_pickup),
            "requires_crew": int(self.accessibility.requires_crew),
            "outage_state": self.outage_state.value,
            "usable_window_count": len(self.usable_windows),
            "policy_version": self.policy_version,
            "authoritative": int(self.authoritative),
        }
        return {key: flat[key] for key in sorted(flat)}


@dataclass(frozen=True)
class ResourceClaim:
    """One unit resource held for one window by one holder."""

    request_id: str
    holder: str
    facility_id: str
    unit: UnitResource
    window: Interval
    purpose: ClaimPurpose
    stages: tuple[TurnaroundStage, ...] = ()

    def __post_init__(self) -> None:
        if self.window.is_empty:
            raise CapacityError(
                f"claim on {self.unit.identifier} for {self.request_id} is empty; "
                "an empty claim is not a resource reservation"
            )
        if self.purpose is not purpose_for_kind(self.unit.kind):
            raise CapacityError(
                f"claim purpose {self.purpose.value} does not match unit kind "
                f"{self.unit.kind.prefix}"
            )

    @property
    def key(self) -> ResourceKey:
        return self.unit.key(self.window)

    @property
    def identity_token(self) -> str:
        return self.unit.identity_token()

    @property
    def duration_us(self) -> int:
        return self.window.measure


@dataclass(frozen=True)
class StageWindow:
    """A stage of the chain placed at an absolute time on a named unit."""

    stage: TurnaroundStage
    window: Interval
    facility_id: str
    unit_identifier: str
    purpose: ClaimPurpose

    @property
    def contains_handover(self) -> bool:
        return self.stage in (TurnaroundStage.HANDOVER, TurnaroundStage.LOADING)


@dataclass(frozen=True)
class WaitEvent:
    """A measured wait, reason-coded at the moment it is discovered."""

    request_id: str
    reason: WaitReason
    from_us: int
    to_us: int
    blocked_on: str

    def __post_init__(self) -> None:
        if self.to_us < self.from_us:
            raise CapacityError("a wait cannot end before it begins")

    @property
    def duration_us(self) -> int:
        return self.to_us - self.from_us


@dataclass(frozen=True)
class TurnaroundRequest:
    """Work a facility is asked to absorb."""

    request_id: str
    facility_id: str
    holder: str
    ready_us: int
    deadline_us: int
    policy_version: str
    handover_type: HandoverType = HandoverType.LAND_TRANSFER
    durations: TurnaroundDurations | None = None
    requires_recharge: bool = True
    recharge_us: int = 0
    preference: int = 0
    allow_queue: bool = True

    def __post_init__(self) -> None:
        require_supported_handover(self.handover_type)
        as_duration_us(self.recharge_us)
        if self.deadline_us <= self.ready_us:
            raise CapacityError(
                f"request {self.request_id} deadline {self.deadline_us} is not after "
                f"ready {self.ready_us}"
            )

    @property
    def arrival_order(self) -> tuple[int, int, str]:
        """The documented deterministic tie-break.

        ``(preference, ready_us, request_id)`` ascending: a lower preference
        number is served first, then the earlier arrival, then the canonical
        request id in byte order. Nothing in this ordering reads a dict, a set
        or a clock, so two replays of the same arrival set grant the same pads.
        """
        return (self.preference, self.ready_us, self.request_id)


@dataclass(frozen=True)
class Rejection:
    """A refusal with a reason, a place to look again, and no side effects."""

    request_id: str
    facility_id: str
    reason: WaitReason
    detail: str
    earliest_retry_us: int | None = None


@dataclass(frozen=True)
class TurnaroundPlan:
    """The proposal: stages placed, claims to commit, waits measured.

    ``proposal_id`` is a digest over the canonical resource keys, so the same
    plan always carries the same identity and a retry cannot create a second
    commitment (I2).
    """

    request_id: str
    facility_id: str
    holder: str
    policy_version: str
    stages: tuple[StageWindow, ...]
    claims: tuple[ResourceClaim, ...]
    waits: tuple[WaitEvent, ...]
    start_us: int
    end_us: int

    @property
    def proposal_id(self) -> str:
        parts = [self.request_id, self.policy_version, *(claim.key.canonical for claim in self.claims)]
        return hashlib.sha256("|".join(sorted(parts)).encode("utf-8")).hexdigest()

    @property
    def windows(self) -> tuple[Interval, ...]:
        return tuple(claim.window for claim in self.claims)

    @property
    def resource_keys(self) -> tuple[str, ...]:
        return tuple(claim.key.canonical for claim in self.claims)

    def claims_for(self, purpose: ClaimPurpose) -> tuple[ResourceClaim, ...]:
        return tuple(claim for claim in self.claims if claim.purpose is purpose)

    def wait_us(self, reason: WaitReason) -> int:
        """Total measured wait attributed to one reason code.

        Pad wait and charger wait are separate calls by design: Phase 4's
        console lists them as distinct queue causes.
        """
        return sum(event.duration_us for event in self.waits if event.reason is reason)

    def as_metrics(self) -> dict[str, str | int]:
        """Metric vector for the golden-scenario comparison."""
        metrics: dict[str, str | int] = {
            "request_id": self.request_id,
            "facility_id": self.facility_id,
            "proposal_id": self.proposal_id,
            "start_us": self.start_us,
            "end_us": self.end_us,
            "claim_count": len(self.claims),
            "stage_count": len(self.stages),
            "pad_claims": len(self.claims_for(ClaimPurpose.PAD)),
            "charger_claims": len(self.claims_for(ClaimPurpose.CHARGER)),
            "crew_claims": len(self.claims_for(ClaimPurpose.CREW)),
            "pad_wait_us": self.wait_us(WaitReason.PAD_WAIT),
            "charger_wait_us": self.wait_us(WaitReason.CHARGER_WAIT),
            "crew_wait_us": self.wait_us(WaitReason.CREW_UNAVAILABLE),
            "turnaround_us": self.end_us - self.start_us,
        }
        return {key: metrics[key] for key in sorted(metrics)}


class DecisionState(str, Enum):
    SCHEDULED = "scheduled"
    QUEUED = "queued"
    REJECTED = "rejected"


@dataclass(frozen=True)
class SchedulingDecision:
    """The scheduler's answer: a proposal, a queue entry, or a rejection."""

    state: DecisionState
    request_id: str
    facility_id: str
    reason: WaitReason
    plan: TurnaroundPlan | None = None
    rejection: Rejection | None = None

    def __post_init__(self) -> None:
        if self.state is DecisionState.SCHEDULED and self.plan is None:
            raise CapacityError("a scheduled decision must carry a plan")
        if self.state is not DecisionState.SCHEDULED and self.plan is not None:
            raise CapacityError("only a scheduled decision may carry a plan")

    @property
    def granted(self) -> bool:
        return self.state is DecisionState.SCHEDULED


def group_claims(claims: Iterable[ResourceClaim]) -> tuple[tuple[str, tuple[ResourceClaim, ...]], ...]:
    """Claims grouped by resource identity, identities in ascending order.

    Grouping is the shape I6 is checked in: for every identity the group must
    be pairwise non-overlapping, and the number of groups of a purpose must
    never exceed that purpose's declared count.
    """
    buckets: dict[str, list[ResourceClaim]] = {}
    for claim in claims:
        buckets.setdefault(claim.identity_token, []).append(claim)
    return tuple(
        (identity, tuple(sorted(buckets[identity], key=lambda item: (item.window.start, item.window.end))))
        for identity in sorted(buckets)
    )


__all__ = [
    "PURPOSE_BY_KIND",
    "SUPPORTED_HANDOVER",
    "TURNAROUND_CHAIN",
    "Accessibility",
    "CapacityProfile",
    "ClaimPurpose",
    "DecisionState",
    "FacilityCapacityView",
    "OutageImpact",
    "OutageState",
    "OutageWindow",
    "Rejection",
    "ResourceClaim",
    "SchedulingDecision",
    "StageWindow",
    "TurnaroundDurations",
    "TurnaroundPlan",
    "TurnaroundRequest",
    "TurnaroundStage",
    "WaitEvent",
    "WaitReason",
    "group_claims",
    "purpose_for_kind",
    "require_supported_handover",
]
