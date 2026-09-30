"""Planner value objects: request, terminals, candidates, commit request, trace.

Everything here is frozen and integer-typed. The shape that matters is
:class:`PlanProposal`: it is a *proposal*, never a verdict. It carries no
``valid``, ``safe`` or ``verified`` field, because the planner is not allowed to
mark its own answer correct (``docs/conventions.md`` section 6, Phase 2 exit
criterion). The payload is written so an independent checker can re-derive the
same conclusions from the plan points and committed records alone.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from droneops_geom_4d import ProfilePoint
from droneops_geom_core import Interval

from .model_util import canonical_digest


class LegKind(StrEnum):
    """The six terminal-model phases of ``technical-build/11`` section 3 step 3."""

    HUB_PAD = "hub_pad"
    CLIMB = "climb"
    CRUISE = "cruise"
    DESCENT = "descent"
    DESTINATION_PAD = "destination_pad"
    RETURN = "return"


class AircraftState(StrEnum):
    """Assignable states only; anything else is rejected before planning."""

    AVAILABLE = "available"
    RESERVED = "reserved"
    FLYING = "flying"
    CHARGING = "charging"
    MAINTENANCE = "maintenance"
    OFFLINE = "offline"
    CONTINGENCY = "contingency"
    RETIRED = "retired"

    @property
    def assignable(self) -> bool:
        return self is AircraftState.AVAILABLE


class PriorityClass(StrEnum):
    ROUTINE = "routine"
    PRIORITY = "priority"
    EMERGENCY = "emergency"


@dataclass(frozen=True)
class PolicySnapshot:
    """The versioned numbers this plan was made under. Nothing is hard-coded.

    Margins are experiment parameters (conventions section 3); the invariant the
    code enforces is that a committed overlap needs an explicit allow rule, not
    that any particular margin is correct.
    """

    policy_version: str
    config_release_sha: str
    horizontal_margin_mm: int
    vertical_margin_mm: int
    position_uncertainty_mm: int
    timing_uncertainty_us: int
    transition_buffer_us: int
    separation_buffer_us: int
    max_agl_mm: int
    max_wind_mm_per_s: int
    wind_energy_bp_per_ms: int
    reserve_fraction_bp: int
    crossing_penalty_units: int
    merge_penalty_units: int
    band_change_units: int
    time_cost_units_per_second: int
    fairness_weight_units: int
    lateness_weight_units_per_minute: int
    energy_weight_units_per_wh: int
    pad_queue_units: int

    def __post_init__(self) -> None:
        for name in (
            "horizontal_margin_mm",
            "vertical_margin_mm",
            "position_uncertainty_mm",
            "max_agl_mm",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be non-negative")
        if self.timing_uncertainty_us < 0 or self.transition_buffer_us < 0:
            raise ValueError("temporal buffers must be non-negative")
        if self.reserve_fraction_bp < 0 or self.reserve_fraction_bp >= 10_000:
            raise ValueError("reserve fraction must be a basis-point ratio below one whole")

    @property
    def uncertainty_and_buffer_us(self) -> int:
        return self.timing_uncertainty_us + self.transition_buffer_us

    def digest(self) -> str:
        return canonical_digest(self)


@dataclass(frozen=True)
class AircraftEnvelope:
    """What this airframe may carry, fly and spend."""

    aircraft_id: str
    profile_id: str
    state: AircraftState
    link_ok: bool
    capacity_wh: int
    charge_wh: int
    cruise_speed_mm_per_s: int
    max_payload_g: int
    climb_rate_mm_per_s: int
    wh_per_100km: int
    home_facility_id: str


@dataclass(frozen=True)
class PlanRequest:
    """One order, in kernel time, with the envelope it must be planned inside."""

    tenant_id: str
    order_id: str
    mission_id: str
    origin_node: str
    destination_node: str
    requested_at_us: int
    deadline_us: int
    cutoff_us: int
    payload_g: int
    priority: PriorityClass
    aircraft: AircraftEnvelope
    seed: int
    candidate_k: int = 4
    idempotency_key: str = ""

    def horizon(self) -> Interval:
        return Interval.of(self.requested_at_us, self.cutoff_us)

    def request_digest(self) -> str:
        return canonical_digest(
            [
                self.tenant_id,
                self.order_id,
                self.mission_id,
                self.origin_node,
                self.destination_node,
                self.requested_at_us,
                self.deadline_us,
                self.cutoff_us,
                self.payload_g,
                self.priority.value,
                self.aircraft.aircraft_id,
                self.seed,
                self.candidate_k,
            ]
        )


@dataclass(frozen=True)
class TerminalLeg:
    """One phase of the terminal model with its reserved window."""

    kind: LegKind
    node_id: str
    window: Interval
    length_mm: int
    from_band: str
    to_band: str

    def digest(self) -> str:
        return canonical_digest(
            [
                self.kind.value,
                self.node_id,
                self.window.start,
                self.window.end,
                self.length_mm,
                self.from_band,
                self.to_band,
            ]
        )


@dataclass(frozen=True)
class PadWindow:
    """A pad or charger bay held for a mission, with the reason it is held."""

    facility_id: str
    resource_kind: str
    index: int
    window: Interval
    purpose: str

    def resource_identifier(self) -> str:
        return f"{self.resource_kind}-{self.facility_id}-{self.index}"

    def digest(self) -> str:
        return canonical_digest(
            [self.facility_id, self.resource_kind, self.index, self.window.start, self.window.end]
        )


@dataclass(frozen=True)
class RouteShape:
    """A candidate lane sequence with the measurable properties of a traversal."""

    edge_ids: tuple[str, ...]
    node_ids: tuple[str, ...]
    length_mm: int
    cruise_time_us: int
    bands: tuple[str, ...]
    merge_nodes: tuple[str, ...]
    crossing_count: int
    is_direct: bool = False

    def canonical(self) -> str:
        return "|".join(self.edge_ids)

    def digest(self) -> str:
        return canonical_digest(
            [
                self.edge_ids,
                self.node_ids,
                self.length_mm,
                self.cruise_time_us,
                self.bands,
                self.merge_nodes,
                self.crossing_count,
                self.is_direct,
            ]
        )


@dataclass(frozen=True)
class ProtectedVolume:
    """One swept cell-time claim: what the plan asks the ledger to hold.

    ``cells`` are geohash3d tokens and ``window`` is the padded occupancy. A
    checker can re-derive both from the route points and the policy without
    reading a single planner object.
    """

    edge_id: str
    cells: tuple[str, ...]
    window: Interval
    band: str

    def digest(self) -> str:
        return canonical_digest([self.edge_id, self.cells, self.window.start, self.window.end])


@dataclass(frozen=True)
class PlanPoint:
    """A 4D point of the proposed route: kernel time, node, band, cumulative distance."""

    at_us: int
    node_id: str
    edge_id: str
    band: str
    cumulative_mm: int

    def digest(self) -> str:
        return canonical_digest(
            [self.at_us, self.node_id, self.edge_id, self.band, self.cumulative_mm]
        )


@dataclass(frozen=True)
class EnergyEvaluation:
    """Consumption, requirement and margin, all in integer watt-hours."""

    consumption_wh: int
    reserve_required_wh: int
    recovery_wh: int
    wind_penalty_wh: int
    climb_wh: int
    available_wh: int
    margin_wh: int

    def digest(self) -> str:
        return canonical_digest(self)


@dataclass(frozen=True)
class Score:
    """The ranking inputs, kept visible rather than collapsed into one number.

    ``total_units`` is what sorts the list; the four components stay in the record
    so a reviewer can reproduce the arithmetic that made one route win.
    """

    lateness_us: int
    energy_wh: int
    disturbance_units: int
    fairness_units: int
    pad_wait_us: int
    total_units: int

    def components(self) -> tuple[tuple[str, int], ...]:
        return (
            ("lateness_us", self.lateness_us),
            ("energy_wh", self.energy_wh),
            ("disturbance_units", self.disturbance_units),
            ("fairness_units", self.fairness_units),
            ("pad_wait_us", self.pad_wait_us),
            ("total_units", self.total_units),
        )

    def digest(self) -> str:
        return canonical_digest(list(self.components()))


@dataclass(frozen=True)
class CandidatePlan:
    """One fully evaluated candidate: route, timing, volumes, energy and score."""

    candidate_id: str
    strategy: str
    route: RouteShape
    entry_us: int
    exit_us: int
    arrival_us: int
    release_us: int
    legs: tuple[TerminalLeg, ...]
    pad_windows: tuple[PadWindow, ...]
    volumes: tuple[ProtectedVolume, ...]
    points: tuple[PlanPoint, ...]
    energy: EnergyEvaluation
    score: Score
    wind_mm_per_s: int
    resource_windows: tuple[tuple[str, int, int], ...]
    lock_order: tuple[str, ...]
    profile_points: tuple[ProfilePoint, ...] = ()
    """Raw 4D waypoints of this candidate: time, place, height with its reference, speed.

    Carried outside the digest on purpose - the digest is the integer decision record,
    these are the geometry a checker re-sweeps from the bundle.
    """

    def digest(self) -> str:
        return canonical_digest(
            [
                self.strategy,
                self.route.digest(),
                self.entry_us,
                self.exit_us,
                self.arrival_us,
                self.release_us,
                [leg.digest() for leg in self.legs],
                [volume.digest() for volume in self.volumes],
                [point.digest() for point in self.points],
                self.energy.digest(),
                self.score.digest(),
                self.wind_mm_per_s,
                list(self.resource_windows),
                list(self.lock_order),
            ]
        )


@dataclass(frozen=True)
class CommitRequest:
    """What step 12 hands to the ledger: a whole plan or nothing.

    ``lock_order`` is pre-sorted in the canonical resource order, and the
    ``reservation_version`` is the version this plan was planned against, so a
    ledger can refuse a stale commit instead of interleaving with it.
    """

    mission_id: str
    tenant_id: str
    candidate_id: str
    policy_version: str
    config_release_sha: str
    graph_version: str
    reservation_version: int
    lock_order: tuple[str, ...]
    resources: tuple[tuple[str, int, int], ...]

    def digest(self) -> str:
        return canonical_digest(
            [
                self.mission_id,
                self.candidate_id,
                self.reservation_version,
                list(self.lock_order),
                list(self.resources),
            ]
        )


class CommitState(StrEnum):
    COMMITTED = "committed"
    REJECTED_STALE_VERSION = "rejected_stale_version"
    REJECTED_CONFLICT = "rejected_conflict"
    REJECTED_INTERNAL = "rejected_internal"


@dataclass(frozen=True)
class CommitOutcome:
    """The ledger's answer. The planner never infers success from silence."""

    state: CommitState
    detail: str
    observed_version: int

    @property
    def committed(self) -> bool:
        return self.state is CommitState.COMMITTED


@dataclass(frozen=True)
class EligibleCandidate:
    """A candidate that survived every check, with the numbers that ranked it."""

    candidate_id: str
    strategy: str
    route_digest: str
    lateness_us: int
    energy_margin_wh: int
    disturbance_units: int
    fairness_units: int
    total_units: int

    def as_row(self) -> tuple[str, int, int, int, int, int]:
        return (
            self.candidate_id,
            self.total_units,
            self.lateness_us,
            self.energy_margin_wh,
            self.disturbance_units,
            self.fairness_units,
        )

    def digest(self) -> str:
        return canonical_digest(list(self.as_row()))


@dataclass(frozen=True)
class StepRecord:
    """One numbered step of the 13-step flow, with what it actually did."""

    step: int
    name: str
    outcome: str
    detail: str
    counters: tuple[tuple[str, int], ...] = ()

    def digest(self) -> str:
        return canonical_digest([self.step, self.name, self.outcome, self.detail, list(self.counters)])


@dataclass(frozen=True)
class RetryRecord:
    """One bounded recompute: what changed, and where the pipeline restarted."""

    attempt: int
    restart_from_step: int
    reservation_version: int
    reason_code: str
    detail: str

    def digest(self) -> str:
        return canonical_digest(
            [self.attempt, self.restart_from_step, self.reservation_version, self.reason_code]
        )


@dataclass(frozen=True)
class DecisionTrace:
    """The explainable record of one planning attempt (spec section 3, step 13)."""

    mission_id: str
    scenario_digest: str
    graph_version: str
    policy_version: str
    config_release_sha: str
    reservation_version: int
    steps: tuple[StepRecord, ...]
    eligible: tuple[EligibleCandidate, ...]
    rejected: tuple[tuple[str, str, str, str], ...]
    ranking: tuple[tuple[str, int, int, int, int, int, int], ...]
    selected_candidate_id: str | None
    retries: tuple[RetryRecord, ...] = ()
    outcome: str = "proposed"
    provenance: tuple[tuple[str, bool], ...] = field(
        default=(("authoritative", False), ("safety_claim", False))
    )

    def digest(self) -> str:
        return canonical_digest(
            [
                self.mission_id,
                self.scenario_digest,
                self.graph_version,
                self.policy_version,
                self.config_release_sha,
                self.reservation_version,
                [step.digest() for step in self.steps],
                [candidate.digest() for candidate in self.eligible],
                [list(row) for row in self.rejected],
                [list(row) for row in self.ranking],
                self.selected_candidate_id,
                [retry.digest() for retry in self.retries],
                self.outcome,
            ]
        )


@dataclass(frozen=True)
class PlanProposal:
    """The planner's whole answer: a proposal, a commit request and a trace.

    Deliberately missing: any field that asserts correctness. Release authority is
    the ledger's and the independent checker's, never this object's.
    """

    proposal_id: str
    mission_id: str
    selected: CandidatePlan | None
    candidates: tuple[CandidatePlan, ...]
    commit_request: CommitRequest | None
    trace: DecisionTrace
    plan_points: tuple[PlanPoint, ...]
    protected_volumes: tuple[ProtectedVolume, ...]
    resources: tuple[tuple[str, int, int], ...]
    authoritative: bool = False
    safety_claim: bool = False
    requires_survey: bool = True

    @property
    def config_release_sha(self) -> str:
        """The config release this proposal was made under, on the proposal itself."""
        return self.trace.config_release_sha

    @property
    def resource_keys(self) -> tuple[str, ...]:
        """Canonical geom-core resource keys this plan asks the ledger to hold."""
        return () if self.selected is None else self.selected.lock_order

    @property
    def raw_profile_points(self) -> tuple[ProfilePoint, ...]:
        """The 4D payload an independent checker needs to re-derive the volumes."""
        return () if self.selected is None else self.selected.profile_points

    @property
    def occupancy_windows(self) -> tuple[tuple[str, int, int], ...]:
        """``(resource identifier, start_us, end_us)`` for every claim in the plan."""
        return self.resources

    def digest(self) -> str:
        return canonical_digest(
            [
                self.proposal_id,
                self.mission_id,
                None if self.selected is None else self.selected.digest(),
                self.trace.digest(),
                [point.digest() for point in self.plan_points],
                [volume.digest() for volume in self.protected_volumes],
                list(self.resources),
            ]
        )


__all__ = [
    "AircraftEnvelope",
    "AircraftState",
    "CandidatePlan",
    "CommitOutcome",
    "CommitRequest",
    "CommitState",
    "DecisionTrace",
    "EligibleCandidate",
    "EnergyEvaluation",
    "LegKind",
    "PadWindow",
    "PlanPoint",
    "PlanProposal",
    "PlanRequest",
    "PolicySnapshot",
    "PriorityClass",
    "ProtectedVolume",
    "RetryRecord",
    "RouteShape",
    "Score",
    "StepRecord",
    "TerminalLeg",
]
