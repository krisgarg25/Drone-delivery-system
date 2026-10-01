"""Wire models for the Phase 4 control-plane surface.

These classes are the realisation of ``contracts/openapi/v1.yaml``: the document names
the surface, the models enforce it. Every response inherits
:class:`~droneops_api.provenance.ProvenanceModel`, so a response cannot be built without
declaring whether it is planned, committed, observed or synthetic, and without the
standing statement that this is simulation only and not authorised for flight.

Requests are ``extra="forbid"``: an undocumented field is a contract change, not a
detail. ``Idempotency-Key`` appears in the header *and* the body of every ``POST`` and
the two must agree - see :mod:`droneops_api.routers`.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from droneops_api.provenance import (
    NOT_AUTHORISED_FOR_FLIGHT,
    SIMULATION_NOTICE,
    SIMULATION_PHASE,
    ProvenanceModel,
    SourceKind,
)

CONFIG_RELEASE_PATTERN = r"^sha256:[0-9a-f]{64}$"
SHA256_PATTERN = r"^[0-9a-f]{64}$"
RESOURCE_KEY_PATTERN = r"^(corridor|volume_cell|crossing|pad|charger_bay|crew):.+$"

PriorityClass = Literal["safety_contingency", "life_critical", "commercial_sla", "routine"]
#: Mirrors ``contracts.openapi.AltitudeReference``: no altitude travels without its datum.
AltitudeReference = Literal["AGL", "AMSL", "GROUND", "PAD"]
MissionStatusValue = Literal[
    "draft",
    "eligible",
    "planning",
    "reserved_pending_commit",
    "accepted",
    "activated",
    "in_progress",
    "complete",
    "cancelled",
    "failed",
    "unresolved",
]
ReservationStateValue = Literal[
    "tentative", "committed", "activated", "completed", "cancelled", "unresolved"
]
ResourceKindValue = Literal["corridor", "volume_cell", "crossing", "pad", "charger_bay", "crew"]
AircraftStatusValue = Literal[
    "registered",
    "available",
    "reserved",
    "loading",
    "flying",
    "unloading",
    "charging",
    "maintenance",
    "offline",
    "retired",
    "contingency",
]
UncertaintyValue = Literal["clear", "uncertain", "lost", "reconciling"]
ErrorCodeValue = Literal[
    "validation_error",
    "duplicate_commit_conflict",
    "idempotency_key_reused",
    "unknown_state",
    "missing_altitude_reference",
    "non_utc_timestamp",
    "forbidden_command_surface",
    "tenant_not_authorized",
    "unresolved",
]


def _config_release(value: str) -> str:
    """A configuration release is content-addressed or it is not a release."""

    if not re.fullmatch(CONFIG_RELEASE_PATTERN, value):
        raise ValueError("config_release_sha must be 'sha256:<64 hex>'")
    return value


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Position(RequestModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    altitude_m: float
    altitude_reference: AltitudeReference


# ------------------------------------------------------------------ orders


class OrderRequest(RequestModel):
    external_ref: str
    origin_facility_id: str
    destination_facility_id: str
    payload_profile_id: str
    priority_class: PriorityClass
    earliest_departure: datetime
    requested_deadline: datetime
    idempotency_key: str = Field(min_length=1)

    @field_validator("earliest_departure", "requested_deadline")
    @classmethod
    def _utc_only(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("non_utc_timestamp: every timestamp must be timezone-aware UTC")
        return value

    def digest(self) -> tuple[object, ...]:
        """Everything except the client key: the identity a reused key must match."""

        return (
            self.external_ref,
            self.origin_facility_id,
            self.destination_facility_id,
            self.payload_profile_id,
            self.priority_class,
            self.earliest_departure,
            self.requested_deadline,
        )


class OrderResponse(ProvenanceModel):
    order_id: UUID
    tenant_id: UUID
    mission_id: UUID
    status: str
    external_ref: str = ""
    priority_class: str = ""


class OrderResponseList(ProvenanceModel):
    items: list[OrderResponse] = Field(default_factory=list)


# ------------------------------------------------------------------ missions


class MissionResponse(ProvenanceModel):
    mission_id: UUID
    tenant_id: UUID
    order_id: UUID | None = None
    aircraft_id: UUID | None = None
    status: MissionStatusValue
    priority_class: str = ""
    policy_version: str = ""
    #: False in every build: there is no flight authority behind this API.
    authorized_for_flight: Literal[False] = False
    config_release_sha: str = ""


class PlanRequest(RequestModel):
    config_release_sha: str
    idempotency_key: str = Field(min_length=1)
    reason_code: str = "operator_requests_plan"

    @field_validator("config_release_sha")
    @classmethod
    def _release(cls, value: str) -> str:
        return _config_release(value)


class PlanVersionResponse(ProvenanceModel):
    plan_version_id: UUID
    mission_id: UUID
    route_hash: str
    energy_margin_wh: int
    config_release_sha: str
    created_at: datetime
    aircraft_id: UUID | None = None
    strategy: str = ""
    rank: int = 0
    resource_keys: list[str] = Field(default_factory=list)
    snapshot_digest: str = ""

    @field_validator("config_release_sha")
    @classmethod
    def _release(cls, value: str) -> str:
        return _config_release(value)


class PlanVersionResponseList(ProvenanceModel):
    items: list[PlanVersionResponse] = Field(default_factory=list)


class ReserveRequest(RequestModel):
    plan_version_id: UUID
    config_release_sha: str
    idempotency_key: str = Field(min_length=1)
    reason_code: str = "operator_commits_plan"

    @field_validator("config_release_sha")
    @classmethod
    def _release(cls, value: str) -> str:
        return _config_release(value)


class ReservationResponse(ProvenanceModel):
    reservation_id: UUID
    tenant_id: UUID
    mission_id: UUID
    resource_key: str = Field(pattern=RESOURCE_KEY_PATTERN)
    resource_kind: ResourceKindValue
    valid_from: datetime
    valid_until: datetime
    state: ReservationStateValue
    commitment_token: str = ""


class ReservationListResponse(ProvenanceModel):
    items: list[ReservationResponse] = Field(default_factory=list)


# ------------------------------------------------------------------ decision explanation


class DecisionInputs(BaseModel):
    """Exactly what the planner was shown, so a reviewer can replay the same question."""

    model_config = ConfigDict(extra="forbid")

    snapshot_digest: str
    policy_version: str
    config_release_sha: str
    origin_facility_id: str
    destination_facility_id: str
    priority_class: str
    earliest_departure: datetime
    requested_deadline: datetime
    reservation_version: int
    restricted_keys: list[str] = Field(default_factory=list)
    world_digest_at_decision: str = ""


class EligibleAircraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rank: int
    aircraft_id: str
    status: str
    link_quality: str
    energy_margin_wh: int
    recovery_margin_wh: int
    lateness_us: int
    disturbance_units: int
    fairness_units: int
    total_units: int


class RejectedCandidate(BaseModel):
    """A refusal an operator can read: a plain reason first, the detail underneath."""

    model_config = ConfigDict(extra="forbid")

    aircraft_id: str = ""
    candidate_id: str = ""
    resource_key: str = ""
    reason_code: str
    human_reason: str
    technical_detail: str
    step: int = 0
    step_name: str = ""


class CandidateRanking(BaseModel):
    model_config = ConfigDict(extra="forbid")

    aircraft_id: str
    candidate_id: str
    strategy: str
    rank: int
    total_units: int
    lateness_us: int
    energy_margin_wh: int


class DecisionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    config_release_sha: str
    reason_codes: list[str] = Field(default_factory=list)
    created_at: datetime
    input_hash: str = ""
    deciding_actor: str = ""
    outcome: str = ""
    committed: bool = False

    @field_validator("config_release_sha")
    @classmethod
    def _release(cls, value: str) -> str:
        return _config_release(value)


class DecisionStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage: str
    outcome: str
    step: int = 0
    detail: str = ""
    energy_margin_wh: int | None = None
    timing_uncertainty_s: int | None = None


class DecisionTraceResponse(ProvenanceModel):
    """The stored trace. Nothing here is recomputed from live tables.

    ``human_reason`` on every rejection is the operator-facing sentence;
    ``technical_detail`` is the evidence behind it. Reading the top rejection should take
    an operator well under the 60 seconds Phase 4 is graded on.
    """

    mission_id: UUID
    tenant_id: UUID
    record: DecisionRecord
    steps: list[DecisionStep] = Field(default_factory=list)
    inputs: DecisionInputs | None = None
    eligible: list[EligibleAircraft] = Field(default_factory=list)
    ranking: list[CandidateRanking] = Field(default_factory=list)
    rejected: list[RejectedCandidate] = Field(default_factory=list)
    selected_plan_version_id: UUID | None = None
    primary_reason_code: str = ""
    primary_human_reason: str = ""
    lock_order: list[str] = Field(default_factory=list)
    explanation_readable_in_s: int = 60


# ------------------------------------------------------------------ monitoring


class TelemetryObservationResponse(ProvenanceModel):
    observation_id: UUID
    tenant_id: UUID
    aircraft_id: UUID
    sequence: int
    measurement_time: datetime
    received_time: datetime
    position: Position
    out_of_order: bool = False
    source: str = "simulated-feed"


class TelemetryListResponse(ProvenanceModel):
    items: list[TelemetryObservationResponse] = Field(default_factory=list)


class UncertaintyStateResponse(ProvenanceModel):
    aircraft_id: UUID
    tenant_id: UUID
    state: UncertaintyValue
    last_accepted_measurement_time: datetime | None = None
    staleness_s: int = 0
    protected_volume_retained: bool = True


# ------------------------------------------------------------------ fleet and facilities


class AircraftResponse(ProvenanceModel):
    aircraft_id: UUID
    tenant_id: UUID
    model_id: UUID
    registration_ref: str
    status: AircraftStatusValue
    link_quality: str = "good"
    base_facility_id: str = ""


class AircraftResponseList(ProvenanceModel):
    items: list[AircraftResponse] = Field(default_factory=list)


class FacilityResponse(ProvenanceModel):
    facility_id: UUID
    tenant_id: UUID
    name: str
    kind: str
    status: str
    outage_reason: str = ""


class FacilityResponseList(ProvenanceModel):
    items: list[FacilityResponse] = Field(default_factory=list)


class FacilityCapacityResponse(ProvenanceModel):
    facility_id: UUID
    pad_capacity: int
    charger_capacity: int
    handover_modes: list[str] = Field(default_factory=list)
    pads_committed: int = 0
    chargers_committed: int = 0
    window_from: datetime | None = None
    window_to: datetime | None = None


class OutageRequest(RequestModel):
    reason_code: str = Field(min_length=1)
    valid_from: datetime
    valid_until: datetime
    idempotency_key: str = Field(min_length=1)

    @field_validator("valid_from", "valid_until")
    @classmethod
    def _utc_only(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("non_utc_timestamp: every timestamp must be timezone-aware UTC")
        return value


class CancelRequest(RequestModel):
    reason_code: str = Field(min_length=1)
    note: str = ""
    idempotency_key: str = Field(min_length=1)


class ProposalDecisionRequest(RequestModel):
    reason_code: str = Field(min_length=1)
    note: str = ""
    idempotency_key: str = Field(min_length=1)


# ------------------------------------------------------------------ audit


class AuditEventResponse(ProvenanceModel):
    audit_id: UUID
    tenant_id: UUID
    occurred_at: datetime
    actor_type: str
    actor_id: str
    operation_kind: str = ""
    resource_type: str = ""
    resource_id: str = ""
    request_id: str = ""
    reason_code: str = ""
    config_release_sha: str = ""
    prev_hash: str = ""
    record_hash: str = ""

    @field_validator("config_release_sha")
    @classmethod
    def _release(cls, value: str) -> str:
        if value == "":
            return value
        return _config_release(value)


class AuditEventListResponse(ProvenanceModel):
    items: list[AuditEventResponse] = Field(default_factory=list)


# ------------------------------------------------------------------ simulation and replay


class RunIdentitySchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bundle_sha256: str
    engine_version: str
    scenario_sha256: str
    policy_version: str
    config_release_sha256: str
    seed: int
    python_version: str
    machine_architecture: str

    @field_validator("config_release_sha256")
    @classmethod
    def _release(cls, value: str) -> str:
        return _config_release(value)


class EconomicsMetricResponse(ProvenanceModel):
    """One economics figure with its formula and its denominator attached.

    ``docs/05`` section 5.8 defines these figures as ratios, so a bare number is not an
    answer: the formula and the denominator travel with the value. ``illustrative`` is set
    on every monetary figure, because the cost model behind it is chapter-7 planning
    arithmetic and has never been validated against a quote.
    """

    key: str
    label: str
    value: str
    unit: str
    formula: str
    denominator: str
    source: str
    illustrative: bool = False


class CostModelResponse(ProvenanceModel):
    """Section 5.9's seven components, labelled illustrative, with its sensitivity table."""

    currency: str = "INR"
    illustrative: bool = True
    caveat: str = ""
    deliveries_per_day_per_hub: int = 0
    fixed_plus_capital_per_drop_inr: int = 0
    variable_per_drop_inr: int = 0
    components: dict[str, int] = Field(default_factory=dict)
    cost_per_completed_delivery_inr: int = 0
    cost_per_completed_delivery_usd: float = 0.0
    road_baseline_inr_low: int = 0
    road_baseline_inr_high: int = 0
    cost_vs_road_baseline: float = 0.0
    sensitivity_inr_per_drop: dict[str, int] = Field(default_factory=dict)


class AircraftChargeState(ProvenanceModel):
    """The state an arm actually flew with: drawn once per seed and shared by every arm."""

    aircraft_id: str
    mission_id: str
    charge_wh: int
    capacity_wh: int
    charge_bp: int
    state: str = ""


class EconomicsResponse(ProvenanceModel):
    """The Economics view of ``docs/05`` 5.4, as one object with its definitions."""

    metrics: list[EconomicsMetricResponse] = Field(default_factory=list)
    submitted_orders: int = 0
    completed: int = 0
    late: int = 0
    rejected: int = 0
    unfinished_at_cutoff: int = 0
    unserved_at_cutoff: int = 0
    deadline_orders: int = 0
    seeds: list[int] = Field(default_factory=list)
    repetitions: int = 0
    energy_wh_median: int = 0
    cost_model: CostModelResponse
    charge_states: list[AircraftChargeState] = Field(default_factory=list)
    illustrative: bool = True


class StrategyLaneRow(ProvenanceModel):
    """One comparison lane: its median and spread over the seed list, not one lucky run."""

    strategy: str
    run_id: str = ""
    repetitions: int = 0
    median_completed: int = 0
    spread_completed: int = 0
    median_energy_wh: int = 0
    spread_energy_wh: int = 0
    median_unserved: int = 0
    spread_unserved: int = 0
    median_pad_wait_us: int = 0
    median_charger_wait_us: int = 0
    median_crew_wait_us: int = 0
    median_cost_inr: int = 0
    demand_digest: str = ""
    inputs_digest: str = ""
    economics: EconomicsResponse | None = None


class SimulationRunResponse(ProvenanceModel):
    run_id: str
    run_identity: RunIdentitySchema
    submitted_orders: int
    accepted: int
    rejected: int
    on_time_rate: float
    snapshot_digest: str = ""
    event_hash: str = ""
    frozen_at: datetime | None = None
    strategy: str = ""
    repetitions: int = 0
    median_seed: int = 0
    economics: EconomicsResponse | None = None


class ReplayDiffResponse(ProvenanceModel):
    run_a: RunIdentitySchema
    run_b: RunIdentitySchema
    event_hash_equal: bool
    metrics_equal: bool
    differing_metrics: list[str] = Field(default_factory=list)
    replayed_against: str = "frozen-snapshot"
    current_world_digest: str = ""


class ScenarioComparisonResponse(ProvenanceModel):
    scenario_id: str
    against: str
    run_a: RunIdentitySchema
    run_b: RunIdentitySchema
    compared_metrics: dict[str, str] = Field(default_factory=dict)
    verdict: str
    seeds: list[int] = Field(default_factory=list)
    repetitions: int = 0
    lanes: list[StrategyLaneRow] = Field(default_factory=list)
    fairness_checks: dict[str, str] = Field(default_factory=dict)
    comparison_error: str = ""


# ------------------------------------------------------------------ status stream


class StatusStreamEvent(ProvenanceModel):
    sequence: int
    occurred_at: datetime
    kind: str
    mission_id: UUID | None = None
    order_id: UUID | None = None
    proposal_id: UUID | None = None
    resource_key: str = ""
    detail: str = ""
    config_release_sha: str = ""


class StatusStreamEventList(ProvenanceModel):
    items: list[StatusStreamEvent] = Field(default_factory=list)
    last_sequence: int = 0


# ------------------------------------------------------------------ errors


class ErrorResponse(ProvenanceModel):
    error_code: ErrorCodeValue
    message: str
    request_id: str = ""

    @staticmethod
    def build(
        error_code: ErrorCodeValue,
        message: str,
        request_id: str,
        *,
        source_kind: SourceKind = SourceKind.SYNTHETIC,
    ) -> ErrorResponse:
        return ErrorResponse(
            error_code=error_code,
            message=message,
            request_id=request_id,
            phase=SIMULATION_PHASE,
            source_kind=source_kind,
            authoritative=False,
            simulation_notice=SIMULATION_NOTICE,
        )


class ConflictResponse(ProvenanceModel):
    error_code: Literal["duplicate_commit_conflict", "idempotency_key_reused"]
    message: str
    existing_resource_id: str
    request_id: str = ""


# ------------------------------------------------------------------ operational


class HealthResponse(BaseModel):
    """Operational liveness. Not part of the reviewed business contract."""

    model_config = ConfigDict(extra="forbid")

    status: str
    phase: str
    control_plane_phase: str = SIMULATION_PHASE
    authorized_for_flight: bool = NOT_AUTHORISED_FOR_FLIGHT
    detail: str = SIMULATION_NOTICE


class VersionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str
    api_version: str
    domain_schema_version: int
    config_release_sha: str = ""
    simulation_notice: str = SIMULATION_NOTICE
