"""Reference control plane: the documented surface, real rules, no database.

This module exists so the contract is *true* rather than *described*. It is a faithful
in-memory implementation of the workflow the ledger runs inside PostgreSQL:

* eligibility and ranking come from ``droneops_reservations`` - the same
  :func:`~droneops_reservations.eligible_aircraft` and
  :func:`~droneops_reservations.rank_candidates` the commit path uses, so a rejection here
  carries the same :class:`~droneops_reservations.RejectionCode` and the same
  human-readable ``explanation`` an operator sees in production;
* airspace claims are :class:`~droneops_reservations.ResourceClaim` values, locked in
  canonical order and self-checked with :func:`~droneops_reservations.find_self_conflict`;
* ids are content-addressed with :func:`~droneops_reservations.deterministic_id`, which is
  what makes a replayed idempotency key land on the same mission instead of a second one;
* the plan is a proposal, and only ``reserve`` commits. Nothing in this module touches an
  aircraft: there is no such operation to reach.

A production deployment injects :mod:`droneops_reservations.ledger` and the
persistence-backed read models in place of these classes. The routers cannot tell the
difference, which is the point: ``droneops_api`` never imports ``droneops_persistence``.
"""

from __future__ import annotations

import hashlib
import json
import platform as host
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import cast

from droneops_api.ports import (
    AuditPort,
    CancelDraft,
    CommitDraft,
    ControlPlaneError,
    DuplicateCommitError,
    FacilityPort,
    FleetPort,
    IdempotencyKeyReusedError,
    MissionPort,
    OrderDraft,
    OrderPort,
    OutageDraft,
    PlanDraft,
    Principal,
    ProposalDraft,
    ProposalPort,
    ReservationPort,
    ResourceNotFoundError,
    SimulationPort,
    StatusPort,
    StoredRequest,
    UnresolvedStateError,
)
from droneops_api.provenance import SourceKind, provenance_fields
from droneops_api.schemas import (
    AircraftResponse,
    AircraftResponseList,
    AuditEventListResponse,
    AuditEventResponse,
    CandidateRanking,
    DecisionInputs,
    DecisionRecord,
    DecisionStep,
    DecisionTraceResponse,
    EligibleAircraft,
    FacilityCapacityResponse,
    FacilityResponse,
    FacilityResponseList,
    MissionResponse,
    OrderResponse,
    OrderResponseList,
    PlanVersionResponse,
    PlanVersionResponseList,
    Position,
    RejectedCandidate,
    ReplayDiffResponse,
    ReservationListResponse,
    ReservationResponse,
    ResourceKindValue,
    RunIdentitySchema,
    ScenarioComparisonResponse,
    SimulationRunResponse,
    StatusStreamEvent,
    TelemetryListResponse,
    TelemetryObservationResponse,
    UncertaintyStateResponse,
    UncertaintyValue,
)
from droneops_contracts import (
    AircraftStatus,
    AltitudeReference,
    LinkQuality,
    MissionStatus,
    PriorityClass,
)
from droneops_core_domain import Mission, create_simulated_mission, transition_mission
from droneops_platform import get_clock
from droneops_reservations import (
    AircraftCandidate,
    CandidatePlan,
    CommitStep,
    EnergyVerdict,
    ProfileSample,
    Rejection,
    RejectionCode,
    ResourceClaim,
    Verdict,
    WorldView,
    deterministic_id,
    eligible_aircraft,
    find_self_conflict,
    in_lock_order,
    lock_tokens,
    rank_candidates,
    reject_restricted,
)

#: The configuration release this simulation deployment is running.
CONFIG_RELEASE_SHA = "sha256:" + hashlib.sha256(b"droneops-phase-4-simulation-release").hexdigest()
POLICY_VERSION = "sim-policy-v1"
ENGINE_VERSION = "sim-1.0.0"
SCENARIO = "chandigarh-synthetic-v1"

ZERO_SHA = "0" * 64
STALE_AFTER_S = 30


def _sha(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def _uuid(namespace: str, *parts: str) -> uuid.UUID:
    return deterministic_id(namespace, *parts)


# ------------------------------------------------------------------ world data


@dataclass
class FacilityRecord:
    facility_id: uuid.UUID
    tenant_id: uuid.UUID
    name: str
    kind: str
    pad_capacity: int
    charger_capacity: int
    handover_modes: tuple[str, ...]
    status: str = "active"
    outage_reason: str = ""
    outages: list[tuple[datetime, datetime, str]] = field(default_factory=list)


@dataclass
class AircraftRecord:
    aircraft_id: uuid.UUID
    tenant_id: uuid.UUID
    model_id: uuid.UUID
    registration_ref: str
    status: AircraftStatus
    link: LinkQuality
    base_facility_id: str
    battery_wh: int
    burn_wh_per_segment: int


@dataclass
class PlanRecord:
    plan_version_id: uuid.UUID
    mission_id: uuid.UUID
    candidate_id: str
    aircraft_id: uuid.UUID
    route_hash: str
    energy_margin_wh: int
    created_at: datetime
    config_release_sha: str
    snapshot_digest: str
    strategy: str
    rank: int
    claims: tuple[ResourceClaim, ...]


@dataclass
class ProposalRecord:
    proposal_id: uuid.UUID
    mission_id: uuid.UUID
    tenant_id: uuid.UUID
    kind: str
    summary: str
    state: str = "pending"
    previewed_at: datetime | None = None
    decided_at: datetime | None = None
    decided_by: str = ""
    reason_code: str = ""
    consequence: str = ""


@dataclass
class MissionRecord:
    mission: Mission
    tenant_id: uuid.UUID
    order_id: uuid.UUID
    external_ref: str
    priority_class: PriorityClass
    earliest_departure: datetime
    requested_deadline: datetime
    payload_profile_id: str
    config_release_sha: str
    aircraft_id: uuid.UUID | None = None
    plans: list[PlanRecord] = field(default_factory=list)
    reservations: list[ReservationResponse] = field(default_factory=list)
    trace: DecisionTraceResponse | None = None
    committed: bool = False
    commitment_token: str = ""


@dataclass
class StoredTrace:
    """The decision record as the ledger would persist it: inputs frozen with the answer."""

    inputs: DecisionInputs
    eligible: list[EligibleAircraft]
    ranking: list[CandidateRanking]
    rejected: list[RejectedCandidate]
    steps: list[DecisionStep]
    reason_codes: list[str]
    created_at: datetime
    committed: bool
    selected_plan_version_id: uuid.UUID | None
    lock_order: list[str]


# ------------------------------------------------------------------ reference plane


class InMemoryControlPlane:
    """Orders, planning, commit, audit, telemetry and replay for one deployment.

    Every read is tenant-filtered by the principal, and a miss in another tenant is
    reported exactly like a miss anywhere: :class:`ResourceNotFoundError`, neutral body, no
    hint that the row exists.
    """

    def __init__(self, tenants: tuple[uuid.UUID, ...] | None = None) -> None:
        self.tenant_a = tenants[0] if tenants else uuid.UUID("22222222-2222-2222-2222-222222222222")
        self.tenant_b = (
            tenants[1]
            if tenants and len(tenants) > 1
            else uuid.UUID("33333333-3333-3333-3333-333333333333")
        )
        self.orders: dict[uuid.UUID, OrderResponse] = {}
        self.missions: dict[uuid.UUID, MissionRecord] = {}
        self.facilities: dict[uuid.UUID, FacilityRecord] = {}
        self.aircraft: dict[uuid.UUID, AircraftRecord] = {}
        self.proposals: dict[uuid.UUID, ProposalRecord] = {}
        self.audit: list[AuditEventResponse] = []
        self.status_log: list[StatusStreamEvent] = []
        self.observation_log: dict[uuid.UUID, list[TelemetryObservationResponse]] = {}
        self.runs: dict[str, SimulationRunResponse] = {}
        self.replays: dict[str, tuple[RunIdentitySchema, str, list[str]]] = {}
        self.committed_claims: list[tuple[uuid.UUID, ResourceClaim, uuid.UUID]] = []
        self.reservation_version = 0
        self._seen_mission_ids: dict[uuid.UUID, uuid.UUID] = {}
        self._seed_world()
        self._seed_runs()

    # ---------------------------------------------------------------- seeding

    def _seed_world(self) -> None:
        for tenant in (self.tenant_a, self.tenant_b):
            for name, kind, pads, chargers in (
                ("hub-north", "hub", 3, 2),
                ("node-hospital-a", "hospital_node", 2, 1),
                ("node-clinic-b", "clinic_node", 1, 1),
            ):
                facility_id = _uuid("facility", str(tenant), name)
                self.facilities[facility_id] = FacilityRecord(
                    facility_id=facility_id,
                    tenant_id=tenant,
                    name=f"{name}-synthetic",
                    kind=kind,
                    pad_capacity=pads,
                    charger_capacity=chargers,
                    handover_modes=("land_transfer", "winch_lower"),
                )
            for index, (registration, status, link, battery) in enumerate(
                (
                    ("TG-SIM-0001", AircraftStatus.AVAILABLE, LinkQuality.GOOD, 1_000),
                    ("TG-SIM-0002", AircraftStatus.AVAILABLE, LinkQuality.GOOD, 860),
                    ("TG-SIM-0003", AircraftStatus.MAINTENANCE, LinkQuality.GOOD, 720),
                    ("TG-SIM-0004", AircraftStatus.AVAILABLE, LinkQuality.LOST, 910),
                ),
                start=1,
            ):
                aircraft_id = _uuid("aircraft", str(tenant), registration)
                self.aircraft[aircraft_id] = AircraftRecord(
                    aircraft_id=aircraft_id,
                    tenant_id=tenant,
                    model_id=_uuid("model", "vx-250"),
                    registration_ref=registration,
                    status=status,
                    link=link,
                    base_facility_id="hub-north",
                    battery_wh=battery,
                    burn_wh_per_segment=140 + 10 * index,
                )

    def _seed_runs(self) -> None:
        for suffix, seed, accepted, rejected, on_time in (
            ("a", 831, 42, 6, 0.91),
            ("b", 907, 39, 9, 0.87),
        ):
            run_id = f"run-{SCENARIO}-policy-{suffix}"
            identity = self._run_identity(run_id, seed, _sha("snapshot", run_id))
            self.runs[run_id] = SimulationRunResponse(
                run_id=run_id,
                run_identity=identity,
                submitted_orders=accepted + rejected,
                accepted=accepted,
                rejected=rejected,
                on_time_rate=on_time,
                snapshot_digest=identity.bundle_sha256,
                event_hash=_sha("events", run_id),
                frozen_at=self._now(),
                **provenance_fields(SourceKind.SYNTHETIC),
            )
            self.replays[run_id] = (identity, _sha("events", run_id), [])

    def _run_identity(self, run_id: str, seed: int, snapshot_digest: str) -> RunIdentitySchema:
        del run_id
        return RunIdentitySchema(
            bundle_sha256=snapshot_digest,
            engine_version=ENGINE_VERSION,
            scenario_sha256=_sha("scenario", SCENARIO),
            policy_version=POLICY_VERSION,
            config_release_sha256=CONFIG_RELEASE_SHA,
            seed=seed,
            python_version=f"{host.python_version_tuple()[0]}.{host.python_version_tuple()[1]}",
            machine_architecture=host.machine(),
        )

    # ---------------------------------------------------------------- clock and tenancy

    @staticmethod
    def _now() -> datetime:
        return get_clock().now()

    def _tenant_mission(self, principal: Principal, mission_id: uuid.UUID) -> MissionRecord:
        record = self.missions.get(mission_id)
        if record is None or record.tenant_id != principal.tenant_id:
            raise ResourceNotFoundError()
        return record

    def _tenant_facility(self, principal: Principal, facility_id: uuid.UUID) -> FacilityRecord:
        record = self.facilities.get(facility_id)
        if record is None or record.tenant_id != principal.tenant_id:
            raise ResourceNotFoundError()
        return record

    # ---------------------------------------------------------------- audit, stream

    def append_audit(
        self,
        principal: Principal,
        *,
        operation_kind: str,
        resource_type: str,
        resource_id: str,
        reason_code: str,
        config_release_sha: str,
    ) -> AuditEventResponse:
        """Every write lands here: actor, reason and configuration release or nothing."""

        prev_hash = self.audit[-1].record_hash if self.audit else ZERO_SHA
        audit_id = _uuid(
            "audit", str(principal.tenant_id), operation_kind, resource_id, str(len(self.audit))
        )
        body = json.dumps(
            {
                "audit_id": str(audit_id),
                "operation_kind": operation_kind,
                "resource_id": resource_id,
                "actor_id": principal.actor_id,
                "reason_code": reason_code,
                "config_release_sha": config_release_sha,
                "prev_hash": prev_hash,
            },
            sort_keys=True,
        )
        event = AuditEventResponse(
            audit_id=audit_id,
            tenant_id=principal.tenant_id,
            occurred_at=self._now(),
            actor_type=principal.actor_type,
            actor_id=principal.actor_id,
            operation_kind=operation_kind,
            resource_type=resource_type,
            resource_id=resource_id,
            request_id=principal.request_id,
            reason_code=reason_code,
            config_release_sha=config_release_sha,
            prev_hash=prev_hash,
            record_hash=_sha(body),
            **provenance_fields(SourceKind.COMMITTED),
        )
        self.audit.append(event)
        return event

    def emit(
        self,
        kind: str,
        *,
        source: SourceKind,
        detail: str,
        mission_id: uuid.UUID | None = None,
        resource_key: str = "",
        order_id: uuid.UUID | None = None,
        proposal_id: uuid.UUID | None = None,
    ) -> None:
        self.status_log.append(
            StatusStreamEvent(
                sequence=len(self.status_log) + 1,
                occurred_at=self._now(),
                kind=kind,
                mission_id=mission_id,
                order_id=order_id,
                proposal_id=proposal_id,
                resource_key=resource_key,
                detail=detail,
                config_release_sha=CONFIG_RELEASE_SHA,
                **provenance_fields(source),
            )
        )

    # ---------------------------------------------------------------- orders

    def create_order(self, principal: Principal, draft: OrderDraft) -> OrderResponse:
        if draft.requested_deadline <= draft.earliest_departure:
            raise ControlPlaneError("requested_deadline must be after earliest_departure")
        if draft.earliest_departure.tzinfo is None:
            raise ControlPlaneError("non_utc_timestamp")
        order_id = _uuid("order", str(principal.tenant_id), draft.idempotency_key)
        mission_id = _uuid("mission", str(principal.tenant_id), draft.idempotency_key)
        if order_id in self.orders:
            return self.orders[order_id]
        priority = PriorityClass(draft.priority_class)
        mission = create_simulated_mission(
            mission_id=mission_id,
            order_id=order_id,
            origin_facility_id=draft.origin_facility_id,
            destination_facility_id=draft.destination_facility_id,
            priority=priority,
            at=self._now(),
        )
        transition_mission(mission, MissionStatus.ELIGIBLE)
        self.missions[mission_id] = MissionRecord(
            mission=mission,
            tenant_id=principal.tenant_id,
            order_id=order_id,
            external_ref=draft.external_ref,
            priority_class=priority,
            earliest_departure=draft.earliest_departure,
            requested_deadline=draft.requested_deadline,
            payload_profile_id=draft.payload_profile_id,
            config_release_sha=principal.config_release_sha,
        )
        response = OrderResponse(
            order_id=order_id,
            tenant_id=principal.tenant_id,
            mission_id=mission_id,
            status="received",
            external_ref=draft.external_ref,
            priority_class=draft.priority_class,
            **provenance_fields(SourceKind.COMMITTED),
        )
        self.orders[order_id] = response
        self.append_audit(
            principal,
            operation_kind="order_received",
            resource_type="order",
            resource_id=str(order_id),
            reason_code="order_accepted_for_planning",
            config_release_sha=principal.config_release_sha,
        )
        self.emit(
            "order.received",
            source=SourceKind.COMMITTED,
            detail=draft.external_ref,
            mission_id=mission_id,
            order_id=order_id,
        )
        return response

    def fetch_order(self, principal: Principal, order_id: uuid.UUID) -> OrderResponse:
        order = self.orders.get(order_id)
        if order is None or order.tenant_id != principal.tenant_id:
            raise ResourceNotFoundError()
        return order

    def list_orders(
        self, principal: Principal, time_from: datetime, time_to: datetime
    ) -> OrderResponseList:
        items = [
            order
            for order in self.orders.values()
            if order.tenant_id == principal.tenant_id
            and self._in_window(self._now(), time_from, time_to)
        ]
        return OrderResponseList(items=items, **provenance_fields(SourceKind.COMMITTED))

    def cancel_order(
        self, principal: Principal, order_id: uuid.UUID, draft: CancelDraft
    ) -> MissionResponse:
        order = self.fetch_order(principal, order_id)
        record = self._tenant_mission(principal, order.mission_id)
        self._cancel_record(principal, record, draft)
        self.orders[order_id] = order.model_copy(update={"status": "cancelled"})
        return self._mission_response(principal, record, SourceKind.COMMITTED)

    # ---------------------------------------------------------------- missions

    def cancel_mission(
        self, principal: Principal, mission_id: uuid.UUID, draft: CancelDraft
    ) -> MissionResponse:
        record = self._tenant_mission(principal, mission_id)
        self._cancel_record(principal, record, draft)
        return self._mission_response(principal, record, SourceKind.COMMITTED)

    def _cancel_record(
        self, principal: Principal, record: MissionRecord, draft: CancelDraft
    ) -> None:
        """Commit wins a race against cancellation; otherwise the cancellation is recorded."""

        if record.mission.status is MissionStatus.CANCELLED:
            return
        if record.committed:
            raise DuplicateCommitError(
                "Mission already committed; cancellation resolves to the commitment.",
                str(record.reservations[0].reservation_id)
                if record.reservations
                else str(record.mission.id),
            )
        if record.mission.status not in (
            MissionStatus.DRAFT,
            MissionStatus.ELIGIBLE,
            MissionStatus.PLANNING,
            MissionStatus.RESERVED_PENDING_COMMIT,
        ):
            raise UnresolvedStateError(f"Mission is {record.mission.status.value}; cannot cancel.")
        transition_mission(record.mission, MissionStatus.CANCELLED)
        for reservation in record.reservations:
            record.reservations[record.reservations.index(reservation)] = reservation.model_copy(
                update={"state": "cancelled"}
            )
        self.append_audit(
            principal,
            operation_kind="mission_cancelled",
            resource_type="mission",
            resource_id=str(record.mission.id),
            reason_code=draft.reason_code,
            config_release_sha=principal.config_release_sha,
        )
        self.emit(
            "mission.cancelled",
            source=SourceKind.COMMITTED,
            detail=draft.reason_code,
            mission_id=record.mission.id,
        )

    def _mission_response(
        self, principal: Principal, record: MissionRecord, source: SourceKind
    ) -> MissionResponse:
        del principal
        return MissionResponse(
            mission_id=record.mission.id,
            tenant_id=record.tenant_id,
            order_id=record.order_id,
            aircraft_id=record.aircraft_id,
            status=record.mission.status.value,
            priority_class=record.priority_class.value,
            policy_version=POLICY_VERSION,
            config_release_sha=record.config_release_sha,
            **provenance_fields(source),
        )

    # ---------------------------------------------------------------- planning

    def plan(
        self, principal: Principal, mission_id: uuid.UUID, draft: PlanDraft
    ) -> PlanVersionResponseList:
        record = self._tenant_mission(principal, mission_id)
        if draft.config_release_sha != CONFIG_RELEASE_SHA:
            self._record_trace(
                record,
                rejected=[
                    self._config_rejection(record, draft.config_release_sha),
                ],
                committed=False,
            )
            return PlanVersionResponseList(items=[], **provenance_fields(SourceKind.PLANNED))
        if record.committed:
            return self._plan_list(record)
        if record.mission.status is MissionStatus.ELIGIBLE:
            transition_mission(record.mission, MissionStatus.PLANNING)

        world = self._world_view(principal, record)
        candidates = self._aircraft_candidates(record)
        eligible, aircraft_rejections = eligible_aircraft(candidates)
        plans = self._candidate_plans(record, eligible, world)
        ranked, plan_rejections = rank_candidates(plans, self._rank_map(eligible))
        rejected = list(aircraft_rejections) + list(plan_rejections)

        accepted: list[PlanRecord] = []
        for rank, plan in enumerate(ranked, start=1):
            outcome = reject_restricted(plan, world)
            if isinstance(outcome, Rejection):
                rejected.append(outcome)
                continue
            self_conflict = find_self_conflict(plan.claims)
            if self_conflict is not None:
                rejected.append(self_conflict)
                continue
            overlap = self._committed_overlap(plan.claims)
            if overlap is not None:
                rejected.append(overlap)
                continue
            accepted.append(
                PlanRecord(
                    plan_version_id=_uuid("plan", str(record.mission.id), plan.candidate_id),
                    mission_id=record.mission.id,
                    candidate_id=plan.candidate_id,
                    aircraft_id=uuid.UUID(plan.aircraft_id),
                    route_hash=_sha(plan.route_digest, str(rank)),
                    energy_margin_wh=self._margin_of(plan, eligible),
                    created_at=self._now(),
                    config_release_sha=world.config_release_sha,
                    snapshot_digest=world.snapshot_digest,
                    strategy=plan.strategy,
                    rank=rank,
                    claims=plan.ordered_claims(),
                )
            )

        record.plans = accepted
        self._record_trace(
            record,
            rejected=rejected,
            committed=False,
            world=world,
            eligible=eligible,
            plans=plans,
            accepted=accepted,
        )
        self.append_audit(
            principal,
            operation_kind="plan_version_generated",
            resource_type="mission",
            resource_id=str(record.mission.id),
            reason_code=rejected[0].code.value if rejected else "plan_generated",
            config_release_sha=world.config_release_sha,
        )
        self.emit(
            "plan.generated",
            source=SourceKind.PLANNED,
            detail=f"{len(accepted)} candidate plans",
            mission_id=record.mission.id,
        )
        self._seed_proposal(record, rejected)
        return self._plan_list(record)

    def plan_versions(self, principal: Principal, mission_id: uuid.UUID) -> PlanVersionResponseList:
        return self._plan_list(self._tenant_mission(principal, mission_id))

    def _plan_list(self, record: MissionRecord) -> PlanVersionResponseList:
        items = [
            PlanVersionResponse(
                plan_version_id=plan.plan_version_id,
                mission_id=plan.mission_id,
                route_hash=plan.route_hash,
                energy_margin_wh=plan.energy_margin_wh,
                config_release_sha=plan.config_release_sha,
                created_at=plan.created_at,
                aircraft_id=plan.aircraft_id,
                strategy=plan.strategy,
                rank=plan.rank,
                resource_keys=[claim.identity for claim in plan.claims],
                snapshot_digest=plan.snapshot_digest,
                **provenance_fields(SourceKind.PLANNED),
            )
            for plan in record.plans
        ]
        return PlanVersionResponseList(items=items, **provenance_fields(SourceKind.PLANNED))

    def _margin_of(self, plan: CandidatePlan, eligible: tuple[AircraftCandidate, ...]) -> int:
        for candidate in eligible:
            if candidate.aircraft_id == plan.aircraft_id:
                return candidate.energy.margin_wh
        return 0

    @staticmethod
    def _rank_map(eligible: tuple[AircraftCandidate, ...]) -> dict[str, int]:
        return {candidate.aircraft_id: index for index, candidate in enumerate(eligible)}

    def _world_view(self, principal: Principal, record: MissionRecord) -> WorldView:
        restricted: set[str] = set()
        for facility in self.facilities.values():
            if facility.tenant_id != principal.tenant_id:
                continue
            for start, end, _reason in facility.outages:
                if start <= record.requested_deadline and end >= record.earliest_departure:
                    base = facility.name.removesuffix("-synthetic")
                    restricted.add(f"pad:{base}")
                    restricted.add(f"pad:{facility.name}")
        restricted.add(
            f"corridor:{record.mission.origin_facility_id}"
            f"~{record.mission.destination_facility_id}~segment-3"
        )
        snapshot_digest = _sha("snapshot", str(principal.tenant_id), str(self.reservation_version))
        return WorldView(
            policy_version=POLICY_VERSION,
            config_release_sha=CONFIG_RELEASE_SHA,
            snapshot_digest=snapshot_digest,
            reservation_version=self.reservation_version,
            restricted_keys=frozenset(restricted),
        )

    def _aircraft_candidates(self, record: MissionRecord) -> tuple[AircraftCandidate, ...]:
        out: list[AircraftCandidate] = []
        for aircraft in sorted(
            (item for item in self.aircraft.values() if item.tenant_id == record.tenant_id),
            key=lambda item: item.registration_ref,
        ):
            available = aircraft.battery_wh
            consumed = aircraft.burn_wh_per_segment * 4
            reserve = max(1, int(available * 0.20))
            margin = available - consumed - reserve
            energy = EnergyVerdict(
                available_wh=available,
                consumed_wh=consumed,
                reserve_floor_wh=reserve,
                recovery_margin_wh=margin // 2,
                recovery_feasible=margin > 0,
                model_version="battery-curve-v1",
            )
            out.append(
                AircraftCandidate(
                    aircraft_id=str(aircraft.aircraft_id),
                    status=aircraft.status,
                    link=aircraft.link,
                    priority_class=record.priority_class,
                    energy=energy,
                    lateness_us=1_000_000 * (2 * aircraft.burn_wh_per_segment % 7),
                    disturbance_units=aircraft.burn_wh_per_segment % 5,
                    fairness_units=len(out) % 3,
                )
            )
        return tuple(out)

    def _candidate_plans(
        self, record: MissionRecord, eligible: tuple[AircraftCandidate, ...], world: WorldView
    ) -> tuple[CandidatePlan, ...]:
        plans: list[CandidatePlan] = []
        for candidate in eligible:
            sample = ProfileSample(
                at=record.earliest_departure,
                lat_deg=30.7305,
                lon_deg=76.7812,
                altitude_m=62.0,
                altitude_reference=AltitudeReference.AGL,
                speed_mm_per_s=11_000,
            )
            for strategy, detour in (("shortest-time", False), ("restricted-avoidance", True)):
                plans.append(
                    CandidatePlan(
                        candidate_id=f"cand-{candidate.aircraft_id}-{strategy}",
                        aircraft_id=candidate.aircraft_id,
                        strategy=strategy,
                        route_digest=_sha(str(record.mission.id), candidate.aircraft_id, strategy),
                        claims=self._claims_for(record, detour),
                        profile=(sample, sample),
                        planner_verdict=Verdict.SAFE,
                        config_release_sha=world.config_release_sha,
                    )
                )
        return tuple(plans)

    def _claims_for(self, record: MissionRecord, detour: bool) -> tuple[ResourceClaim, ...]:
        origin = record.mission.origin_facility_id
        destination = record.mission.destination_facility_id
        start = record.earliest_departure
        segments = (1, 2, 4, 5) if detour else (1, 2, 3, 4)
        claims: list[ResourceClaim] = [
            ResourceClaim.of("pad", origin, start, start + timedelta(seconds=45))
        ]
        for index, segment in enumerate(segments):
            at = start + timedelta(seconds=60 * (index + 1))
            claims.append(
                ResourceClaim.of(
                    "corridor",
                    f"{origin}~{destination}~segment-{segment}",
                    at,
                    at + timedelta(seconds=55),
                )
            )
        arrive = start + timedelta(minutes=5)
        claims.append(ResourceClaim.of("pad", destination, arrive, arrive + timedelta(seconds=45)))
        claims.append(
            ResourceClaim.of(
                "charger_bay", f"{destination}~bay-1", arrive, arrive + timedelta(minutes=20)
            )
        )
        return in_lock_order(claims)

    def _committed_overlap(self, claims: tuple[ResourceClaim, ...]) -> Rejection | None:
        for _tenant, committed, reservation_id in self.committed_claims:
            for claim in claims:
                if claim.conflicts_with(committed):
                    return Rejection(
                        RejectionCode.COMMITTED_OVERLAP,
                        f"{claim.identity} is already committed by reservation {reservation_id}",
                        resource_key=claim.stored_key,
                    )
        return None

    # ---------------------------------------------------------------- commit

    def reserve(
        self, principal: Principal, mission_id: uuid.UUID, draft: CommitDraft
    ) -> ReservationListResponse:
        record = self._tenant_mission(principal, mission_id)
        if record.committed:
            if record.commitment_token == self._commitment_token(mission_id, draft.plan_version_id):
                return ReservationListResponse(
                    items=list(record.reservations), **provenance_fields(SourceKind.COMMITTED)
                )
            raise DuplicateCommitError(
                "A different plan version was requested for an already committed mission.",
                str(record.reservations[0].reservation_id)
                if record.reservations
                else str(mission_id),
            )
        plan = next(
            (item for item in record.plans if item.plan_version_id == draft.plan_version_id), None
        )
        if plan is None:
            raise ResourceNotFoundError("Plan version not found for this mission")
        if draft.config_release_sha != plan.config_release_sha:
            raise UnresolvedStateError(
                "config_release_sha does not match the plan version; re-plan before committing."
            )
        world = self._world_view(principal, record)
        candidates = self._aircraft_candidates(record)
        eligible, aircraft_rejections = eligible_aircraft(candidates)
        # Only rejections that concern the aircraft this plan version asks for can block
        # the commit; the others belong to the trace, not to the answer.
        selected = str(plan.aircraft_id)
        rejected: list[Rejection] = [
            item for item in aircraft_rejections if item.aircraft_id in (selected, None)
        ]
        if not any(item.aircraft_id == selected for item in eligible):
            rejected.append(
                Rejection(
                    RejectionCode.AIRCRAFT_NOT_OPERABLE,
                    f"aircraft {selected} is not eligible for a new commitment",
                    aircraft_id=selected,
                    candidate_id=plan.candidate_id,
                )
            )
        restricted = reject_restricted(self._plan_of(record, plan, world), world)
        if isinstance(restricted, Rejection):
            rejected.append(restricted)
        else:
            overlap = self._committed_overlap(plan.claims)
            if overlap is not None:
                rejected.append(overlap)

        if rejected:
            self._record_trace(
                record,
                rejected=rejected,
                committed=False,
                world=world,
                eligible=eligible,
                plans=(),
                accepted=record.plans,
            )
            self.append_audit(
                principal,
                operation_kind="commit_rejected",
                resource_type="mission",
                resource_id=str(mission_id),
                reason_code=rejected[0].code.value,
                config_release_sha=world.config_release_sha,
            )
            raise DuplicateCommitError(
                f"Commit refused: {rejected[0].explain()}",
                str(self._conflicting_reservation(rejected[0])) or str(mission_id),
            )

        token = self._commitment_token(mission_id, plan.plan_version_id)
        record.commitment_token = token
        record.reservations = [
            ReservationResponse(
                reservation_id=_uuid("reservation", token, claim.identity),
                tenant_id=record.tenant_id,
                mission_id=mission_id,
                resource_key=claim.identity,
                resource_kind=cast(ResourceKindValue, claim.identity.split(":", 1)[0]),
                valid_from=claim.valid_from,
                valid_until=claim.valid_until,
                state="committed",
                commitment_token=token,
                **provenance_fields(SourceKind.COMMITTED),
            )
            for claim in plan.claims
        ]
        for claim in plan.claims:
            self.committed_claims.append(
                (record.tenant_id, claim, record.reservations[0].reservation_id)
            )
        self.reservation_version += 1
        record.aircraft_id = plan.aircraft_id
        if record.mission.status is MissionStatus.PLANNING:
            transition_mission(record.mission, MissionStatus.RESERVED_PENDING_COMMIT)
            transition_mission(record.mission, MissionStatus.ACCEPTED)
        record.committed = True
        self._record_trace(
            record,
            rejected=rejected,
            committed=True,
            world=world,
            eligible=eligible,
            plans=(),
            accepted=record.plans,
            lock_order=list(lock_tokens(plan.claims)),
            selected=plan.plan_version_id,
        )
        self.append_audit(
            principal,
            operation_kind="reservation_committed",
            resource_type="mission",
            resource_id=str(mission_id),
            reason_code="committed",
            config_release_sha=world.config_release_sha,
        )
        for reservation in record.reservations:
            self.emit(
                "reservation.committed",
                source=SourceKind.COMMITTED,
                detail=reservation.resource_key,
                mission_id=mission_id,
                resource_key=reservation.resource_key,
            )
        return ReservationListResponse(
            items=list(record.reservations), **provenance_fields(SourceKind.COMMITTED)
        )

    def _conflicting_reservation(self, rejection: Rejection) -> uuid.UUID | None:
        if rejection.resource_key:
            for _tenant, claim, reservation_id in self.committed_claims:
                if claim.stored_key == rejection.resource_key:
                    return reservation_id
        return None

    @staticmethod
    def _commitment_token(mission_id: uuid.UUID, plan_version_id: uuid.UUID) -> str:
        return f"ct-{_sha('commitment', str(mission_id), str(plan_version_id))[:32]}"

    def _plan_of(self, record: MissionRecord, plan: PlanRecord, world: WorldView) -> CandidatePlan:
        return CandidatePlan(
            candidate_id=plan.candidate_id,
            aircraft_id=str(plan.aircraft_id),
            strategy=plan.strategy,
            route_digest=plan.route_hash,
            claims=plan.claims,
            profile=(
                ProfileSample(
                    at=record.earliest_departure,
                    lat_deg=30.7305,
                    lon_deg=76.7812,
                    altitude_m=62.0,
                    altitude_reference=AltitudeReference.AGL,
                ),
            ),
            planner_verdict=Verdict.SAFE,
            config_release_sha=world.config_release_sha,
        )

    def reservations(self, principal: Principal, mission_id: uuid.UUID) -> ReservationListResponse:
        record = self._tenant_mission(principal, mission_id)
        return ReservationListResponse(
            items=list(record.reservations), **provenance_fields(SourceKind.COMMITTED)
        )

    def list_reservations(
        self, principal: Principal, time_from: datetime, time_to: datetime
    ) -> ReservationListResponse:
        items = [
            reservation
            for record in self.missions.values()
            if record.tenant_id == principal.tenant_id
            for reservation in record.reservations
            if self._in_window(reservation.valid_from, time_from, time_to)
        ]
        return ReservationListResponse(items=items, **provenance_fields(SourceKind.COMMITTED))

    @staticmethod
    def _in_window(moment: datetime, time_from: datetime, time_to: datetime) -> bool:
        return time_from <= moment <= time_to

    # ---------------------------------------------------------------- decision trace

    def decision(self, principal: Principal, mission_id: uuid.UUID) -> DecisionTraceResponse:
        record = self._tenant_mission(principal, mission_id)
        if record.trace is None:
            raise ResourceNotFoundError("No decision recorded for this mission yet")
        return record.trace

    def _config_rejection(self, record: MissionRecord, requested: str) -> RejectedCandidate:
        return RejectedCandidate(
            reason_code="config.config_release_mismatch",
            human_reason=(
                "The plan was requested against a configuration release this deployment is not "
                "running, so no candidate can be trusted."
            ),
            technical_detail=(
                f"requested={requested} running={CONFIG_RELEASE_SHA} "
                f"policy={POLICY_VERSION} mission={record.mission.id}"
            ),
            step=2,
            step_name="freeze_world_snapshot",
        )

    def _rejection_view(self, rejection: Rejection) -> RejectedCandidate:
        step = self._step_for(rejection.code)
        return RejectedCandidate(
            aircraft_id=rejection.aircraft_id or "",
            candidate_id=rejection.candidate_id or "",
            resource_key=rejection.resource_key or "",
            reason_code=rejection.code.value,
            human_reason=rejection.code.explanation,
            technical_detail=(
                f"{rejection.detail} | code={rejection.code.value}"
                f" resource={rejection.resource_key or '-'}"
                f" candidate={rejection.candidate_id or '-'}"
            ),
            step=step.number,
            step_name=step.value,
        )

    @staticmethod
    def _step_for(code: RejectionCode) -> CommitStep:
        mapping = {
            RejectionCode.AIRCRAFT_NOT_OPERABLE: CommitStep.RANK_ELIGIBLE_AIRCRAFT,
            RejectionCode.NO_ELIGIBLE_AIRCRAFT: CommitStep.RANK_ELIGIBLE_AIRCRAFT,
            RejectionCode.CANDIDATE_RESTRICTED: CommitStep.REJECT_RESTRICTED_CANDIDATES,
            RejectionCode.CANDIDATE_SELF_OVERLAP: CommitStep.GENERATE_CANDIDATE_ROUTES,
            RejectionCode.NO_CANDIDATE_PLANS: CommitStep.GENERATE_CANDIDATE_ROUTES,
            RejectionCode.ENERGY_MARGIN_BELOW_RESERVE: CommitStep.APPLY_ENERGY_AND_RECOVERY_CHECKS,
            RejectionCode.RECOVERY_NOT_FEASIBLE: CommitStep.APPLY_ENERGY_AND_RECOVERY_CHECKS,
            RejectionCode.COMMITTED_OVERLAP: CommitStep.RECHECK_COMMITTED_RESERVATIONS,
            RejectionCode.UNRESOLVED_PROTECTION: CommitStep.RECHECK_COMMITTED_RESERVATIONS,
            RejectionCode.RESOURCE_UNAVAILABLE: CommitStep.FREEZE_WORLD_SNAPSHOT,
            RejectionCode.SNAPSHOT_NOT_FROZEN: CommitStep.FREEZE_WORLD_SNAPSHOT,
            RejectionCode.LOCK_ORDER_INVALID: CommitStep.ACQUIRE_ROW_LOCKS,
            RejectionCode.STALE_RESERVATION_VERSION: CommitStep.ACQUIRE_ROW_LOCKS,
            RejectionCode.IDENTITY_MISMATCH: CommitStep.VALIDATE_DUPLICATE_IDENTITY,
            RejectionCode.DUPLICATE_REQUEST_RESOLVED: CommitStep.VALIDATE_DUPLICATE_IDENTITY,
        }
        return mapping.get(code, CommitStep.COMMIT_OR_ROLLBACK)

    def _record_trace(
        self,
        record: MissionRecord,
        *,
        rejected: Sequence[RejectedCandidate | Rejection],
        committed: bool,
        world: WorldView | None = None,
        eligible: tuple[AircraftCandidate, ...] = (),
        plans: tuple[CandidatePlan, ...] = (),
        accepted: list[PlanRecord] | None = None,
        lock_order: list[str] | None = None,
        selected: uuid.UUID | None = None,
    ) -> None:
        views = [
            item if isinstance(item, RejectedCandidate) else self._rejection_view(item)
            for item in rejected
        ]
        # The reason an operator is looking for is the one that stopped a candidate route,
        # not the fleet noise beside it: candidate and resource rejections come first, and
        # of those the furthest step reached is the binding cause.
        views.sort(
            key=lambda view: (0 if (view.resource_key or view.candidate_id) else 1, -view.step)
        )
        release = world.config_release_sha if world else CONFIG_RELEASE_SHA
        inputs = DecisionInputs(
            snapshot_digest=world.snapshot_digest
            if world
            else _sha("snapshot", str(record.mission.id)),
            policy_version=POLICY_VERSION,
            config_release_sha=release,
            origin_facility_id=record.mission.origin_facility_id,
            destination_facility_id=record.mission.destination_facility_id,
            priority_class=record.priority_class.value,
            earliest_departure=record.earliest_departure,
            requested_deadline=record.requested_deadline,
            reservation_version=self.reservation_version,
            restricted_keys=sorted(world.restricted_keys) if world else [],
            world_digest_at_decision=_sha("world", str(self.reservation_version)),
        )
        eligible_views = [
            EligibleAircraft(
                rank=index,
                aircraft_id=candidate.aircraft_id,
                status=candidate.status.value,
                link_quality=candidate.link.value,
                energy_margin_wh=candidate.energy.margin_wh,
                recovery_margin_wh=candidate.energy.recovery_margin_wh,
                lateness_us=candidate.lateness_us,
                disturbance_units=candidate.disturbance_units,
                fairness_units=candidate.fairness_units,
                total_units=candidate.total_units,
            )
            for index, candidate in enumerate(eligible, start=1)
        ]
        ranking_views = [
            CandidateRanking(
                aircraft_id=plan.aircraft_id,
                candidate_id=plan.candidate_id,
                strategy=plan.strategy,
                rank=index,
                total_units=next(
                    (c.total_units for c in eligible if c.aircraft_id == plan.aircraft_id), 0
                ),
                lateness_us=next(
                    (c.lateness_us for c in eligible if c.aircraft_id == plan.aircraft_id), 0
                ),
                energy_margin_wh=next(
                    (c.energy.margin_wh for c in eligible if c.aircraft_id == plan.aircraft_id), 0
                ),
            )
            for index, plan in enumerate(plans, start=1)
        ]
        steps: list[DecisionStep] = []
        for step in CommitStep:
            detail = step.description
            if step is CommitStep.RECHECK_COMMITTED_RESERVATIONS and accepted:
                detail = f"{len(accepted)} candidate plans survived; lock order prepared"
            steps.append(
                DecisionStep(
                    stage=step.value,
                    outcome="passed"
                    if not any(v.step_name == step.value for v in views)
                    else "rejected",
                    step=step.number,
                    detail=detail,
                    energy_margin_wh=accepted[0].energy_margin_wh if accepted else None,
                    timing_uncertainty_s=5,
                )
            )
        previous = record.trace
        if previous is not None:
            # Committing does not erase the explanation that produced the commitment: the
            # plan-stage ranking and rejections stay readable beside the committed record.
            eligible_views = eligible_views or previous.eligible
            ranking_views = ranking_views or previous.ranking
            views = views or previous.rejected
            lock_order = lock_order or previous.lock_order
        reason_codes = [view.reason_code for view in views] or (
            ["ledger.committed"] if committed else ["plan.generated"]
        )
        primary_code = "ledger.committed" if committed else reason_codes[0]
        primary_human = (
            "All checks passed: the ledger committed the selected plan version."
            if committed
            else (
                views[0].human_reason
                if views
                else "Candidate plans were generated; nothing was rejected in this pass."
            )
        )
        record.trace = DecisionTraceResponse(
            mission_id=record.mission.id,
            tenant_id=record.tenant_id,
            record=DecisionRecord(
                config_release_sha=release,
                reason_codes=reason_codes,
                created_at=self._now(),
                input_hash=_sha(inputs.snapshot_digest, str(record.mission.id), release),
                deciding_actor="reservation_ledger",
                outcome="committed" if committed else "rejected" if views else "planned",
                committed=committed,
            ),
            steps=steps,
            inputs=inputs,
            eligible=eligible_views,
            ranking=ranking_views,
            rejected=views,
            selected_plan_version_id=selected,
            primary_reason_code=primary_code,
            primary_human_reason=primary_human,
            lock_order=lock_order or [],
            **provenance_fields(SourceKind.COMMITTED if committed else SourceKind.PLANNED),
        )

    # ---------------------------------------------------------------- monitoring

    def observations(self, principal: Principal, mission_id: uuid.UUID) -> TelemetryListResponse:
        record = self._tenant_mission(principal, mission_id)
        seeded = self.observation_log.get(record.mission.id)
        if seeded is None:
            aircraft_id = record.aircraft_id or next(
                item.aircraft_id
                for item in self.aircraft.values()
                if item.tenant_id == record.tenant_id
            )
            base = self._now() - timedelta(seconds=90)
            seeded = []
            newest_measurement = base
            for index, offset in enumerate((0, 1, 2, 3)):
                measurement = base + timedelta(seconds=10 * offset)
                received = measurement + timedelta(seconds=1 if offset != 2 else 9)
                out_of_order = offset == 2
                if out_of_order:
                    measurement = newest_measurement - timedelta(seconds=5)
                else:
                    newest_measurement = measurement
                seeded.append(
                    TelemetryObservationResponse(
                        observation_id=_uuid("obs", str(record.mission.id), str(index)),
                        tenant_id=record.tenant_id,
                        aircraft_id=aircraft_id,
                        sequence=400 + index,
                        measurement_time=measurement,
                        received_time=received,
                        position=Position(
                            lat=30.7305 + 0.001 * index,
                            lon=76.7812 + 0.001 * index,
                            altitude_m=62.0,
                            altitude_reference="AGL",
                        ),
                        out_of_order=out_of_order,
                        **provenance_fields(SourceKind.OBSERVED),
                    )
                )
            self.observation_log[record.mission.id] = seeded
        return TelemetryListResponse(items=list(seeded), **provenance_fields(SourceKind.OBSERVED))

    def uncertainty(self, principal: Principal, mission_id: uuid.UUID) -> UncertaintyStateResponse:
        record = self._tenant_mission(principal, mission_id)
        items = self.observations(principal, mission_id).items
        newest = max((item.measurement_time for item in items), default=self._now())
        aircraft_id = record.aircraft_id or items[0].aircraft_id
        staleness = max(0, int((self._now() - newest).total_seconds()))
        state: UncertaintyValue = "clear"
        if staleness > STALE_AFTER_S * 2:
            state = "lost"
        elif staleness > STALE_AFTER_S:
            state = "uncertain"
        return UncertaintyStateResponse(
            aircraft_id=aircraft_id,
            tenant_id=record.tenant_id,
            state=state,
            last_accepted_measurement_time=newest,
            staleness_s=staleness,
            protected_volume_retained=True,
            **provenance_fields(SourceKind.OBSERVED),
        )

    # ---------------------------------------------------------------- proposals

    def _seed_proposal(self, record: MissionRecord, rejected: list[Rejection]) -> None:
        if not rejected or record.committed:
            return
        proposal_id = _uuid("proposal", str(record.mission.id), "replan-after-rejection")
        if proposal_id in self.proposals:
            return
        self.proposals[proposal_id] = ProposalRecord(
            proposal_id=proposal_id,
            mission_id=record.mission.id,
            tenant_id=record.tenant_id,
            kind="replan_after_rejection",
            summary=(
                f"Re-plan {record.mission.id} with the next eligible aircraft after "
                f"{rejected[0].code.value}."
            ),
            consequence="Changes the queued workflow record only; no aircraft is tasked.",
        )
        self.emit(
            "proposal.raised",
            source=SourceKind.PLANNED,
            detail=rejected[0].explain(),
            mission_id=record.mission.id,
            proposal_id=proposal_id,
        )

    def _proposal(
        self, principal: Principal, mission_id: uuid.UUID, proposal_id: uuid.UUID
    ) -> ProposalRecord:
        proposal = self.proposals.get(proposal_id)
        if proposal is None or proposal.mission_id != mission_id:
            raise ResourceNotFoundError()
        self._tenant_mission(principal, mission_id)
        if proposal.tenant_id != principal.tenant_id:
            raise ResourceNotFoundError()
        return proposal

    def preview_proposal(
        self,
        principal: Principal,
        mission_id: uuid.UUID,
        proposal_id: uuid.UUID,
        draft: ProposalDraft,
    ) -> MissionResponse:
        record = self._tenant_mission(principal, mission_id)
        proposal = self._proposal(principal, mission_id, proposal_id)
        proposal.previewed_at = self._now()
        proposal.state = "previewed" if proposal.state == "pending" else proposal.state
        self.append_audit(
            principal,
            operation_kind="proposal_previewed",
            resource_type="proposal",
            resource_id=str(proposal_id),
            reason_code=draft.reason_code,
            config_release_sha=principal.config_release_sha,
        )
        self.emit(
            "proposal.previewed",
            source=SourceKind.PLANNED,
            detail=proposal.summary,
            mission_id=mission_id,
        )
        return self._mission_response(principal, record, SourceKind.PLANNED)

    def accept_proposal(
        self,
        principal: Principal,
        mission_id: uuid.UUID,
        proposal_id: uuid.UUID,
        draft: ProposalDraft,
    ) -> MissionResponse:
        record = self._tenant_mission(principal, mission_id)
        proposal = self._proposal(principal, mission_id, proposal_id)
        if proposal.state in ("accepted", "declined", "expired"):
            raise DuplicateCommitError(f"Proposal is already {proposal.state}.", str(proposal_id))
        proposal.state = "accepted"
        proposal.decided_at = self._now()
        proposal.decided_by = principal.actor_id
        proposal.reason_code = draft.reason_code
        self.append_audit(
            principal,
            operation_kind="proposal_accepted",
            resource_type="proposal",
            resource_id=str(proposal_id),
            reason_code=draft.reason_code,
            config_release_sha=principal.config_release_sha,
        )
        self.emit(
            "proposal.accepted",
            source=SourceKind.COMMITTED,
            detail=proposal.summary,
            mission_id=mission_id,
        )
        return self._mission_response(principal, record, SourceKind.COMMITTED)

    def decline_proposal(
        self,
        principal: Principal,
        mission_id: uuid.UUID,
        proposal_id: uuid.UUID,
        draft: ProposalDraft,
    ) -> MissionResponse:
        record = self._tenant_mission(principal, mission_id)
        proposal = self._proposal(principal, mission_id, proposal_id)
        if proposal.state in ("accepted", "declined", "expired"):
            raise DuplicateCommitError(f"Proposal is already {proposal.state}.", str(proposal_id))
        proposal.state = "declined"
        proposal.decided_at = self._now()
        proposal.decided_by = principal.actor_id
        proposal.reason_code = draft.reason_code
        self.append_audit(
            principal,
            operation_kind="proposal_declined",
            resource_type="proposal",
            resource_id=str(proposal_id),
            reason_code=draft.reason_code,
            config_release_sha=principal.config_release_sha,
        )
        self.emit(
            "proposal.declined",
            source=SourceKind.COMMITTED,
            detail=draft.reason_code,
            mission_id=mission_id,
        )
        return self._mission_response(principal, record, SourceKind.COMMITTED)

    def proposal_ids(self, principal: Principal, mission_id: uuid.UUID) -> list[uuid.UUID]:
        self._tenant_mission(principal, mission_id)
        return sorted(
            (
                proposal.proposal_id
                for proposal in self.proposals.values()
                if proposal.mission_id == mission_id and proposal.tenant_id == principal.tenant_id
            ),
            key=str,
        )

    # ---------------------------------------------------------------- fleet and facilities

    def list_aircraft(self, principal: Principal) -> AircraftResponseList:
        return AircraftResponseList(
            items=[
                self._aircraft_response(principal, item)
                for item in self._tenant_aircraft(principal)
            ],
            **provenance_fields(SourceKind.SYNTHETIC),
        )

    def fetch_aircraft(self, principal: Principal, aircraft_id: uuid.UUID) -> AircraftResponse:
        item = self.aircraft.get(aircraft_id)
        if item is None or item.tenant_id != principal.tenant_id:
            raise ResourceNotFoundError()
        return self._aircraft_response(principal, item)

    def _tenant_aircraft(self, principal: Principal) -> list[AircraftRecord]:
        return sorted(
            (item for item in self.aircraft.values() if item.tenant_id == principal.tenant_id),
            key=lambda item: item.registration_ref,
        )

    def _aircraft_response(self, principal: Principal, item: AircraftRecord) -> AircraftResponse:
        del principal
        return AircraftResponse(
            aircraft_id=item.aircraft_id,
            tenant_id=item.tenant_id,
            model_id=item.model_id,
            registration_ref=item.registration_ref,
            status=item.status.value,
            link_quality=item.link.value,
            base_facility_id=item.base_facility_id,
            **provenance_fields(SourceKind.SYNTHETIC),
        )

    def list_facilities(self, principal: Principal) -> FacilityResponseList:
        return FacilityResponseList(
            items=[
                self._facility_response(item)
                for item in sorted(
                    (f for f in self.facilities.values() if f.tenant_id == principal.tenant_id),
                    key=lambda f: f.name,
                )
            ],
            **provenance_fields(SourceKind.SYNTHETIC),
        )

    def _facility_response(self, item: FacilityRecord) -> FacilityResponse:
        return FacilityResponse(
            facility_id=item.facility_id,
            tenant_id=item.tenant_id,
            name=item.name,
            kind=item.kind,
            status=item.status,
            outage_reason=item.outage_reason,
            **provenance_fields(SourceKind.SYNTHETIC),
        )

    def capacity(self, principal: Principal, facility_id: uuid.UUID) -> FacilityCapacityResponse:
        item = self._tenant_facility(principal, facility_id)
        bases = (item.name, item.name.removesuffix("-synthetic"))
        pads = sum(
            1
            for _tenant, claim, _rid in self.committed_claims
            if any(claim.identity == f"pad:{base}" for base in bases)
        )
        chargers = sum(
            1
            for _tenant, claim, _rid in self.committed_claims
            if any(claim.identity.startswith(f"charger_bay:{base}") for base in bases)
        )
        return FacilityCapacityResponse(
            facility_id=item.facility_id,
            pad_capacity=item.pad_capacity,
            charger_capacity=item.charger_capacity,
            handover_modes=list(item.handover_modes),
            pads_committed=pads,
            chargers_committed=chargers,
            **provenance_fields(SourceKind.SYNTHETIC),
        )

    def report_outage(
        self, principal: Principal, facility_id: uuid.UUID, draft: OutageDraft
    ) -> FacilityResponse:
        item = self._tenant_facility(principal, facility_id)
        if draft.valid_until <= draft.valid_from:
            raise ControlPlaneError("Outage window must be increasing")
        item.outages.append((draft.valid_from, draft.valid_until, draft.reason_code))
        item.status = "outage"
        item.outage_reason = draft.reason_code
        self.append_audit(
            principal,
            operation_kind="facility_outage_recorded",
            resource_type="facility",
            resource_id=str(facility_id),
            reason_code=draft.reason_code,
            config_release_sha=principal.config_release_sha,
        )
        self.emit(
            "facility.outage",
            source=SourceKind.COMMITTED,
            detail=draft.reason_code,
            resource_key=f"pad:{item.name}",
        )
        return self._facility_response(item)

    # ---------------------------------------------------------------- audit and replay

    def list_audit(
        self, principal: Principal, time_from: datetime, time_to: datetime
    ) -> AuditEventListResponse:
        items = [
            event
            for event in self.audit
            if event.tenant_id == principal.tenant_id
            and self._in_window(event.occurred_at, time_from, time_to)
        ]
        return AuditEventListResponse(items=items, **provenance_fields(SourceKind.COMMITTED))

    def events(
        self, principal: Principal, after_sequence: int, limit: int
    ) -> list[StatusStreamEvent]:
        del principal
        return [event for event in self.status_log if event.sequence > after_sequence][:limit]

    def run(self, principal: Principal, run_id: str) -> SimulationRunResponse:
        del principal
        run = self.runs.get(run_id)
        if run is None:
            raise ResourceNotFoundError()
        return run

    def replay_diff(self, principal: Principal, run_id: str) -> ReplayDiffResponse:
        """Replay the frozen snapshot the run actually used, never the live world."""

        del principal
        stored = self.replays.get(run_id)
        if stored is None:
            raise ResourceNotFoundError()
        frozen_identity, event_hash, _ = stored
        replayed = self._run_identity(run_id, frozen_identity.seed, frozen_identity.bundle_sha256)
        replay_hash = _sha("events", run_id)
        return ReplayDiffResponse(
            run_a=frozen_identity,
            run_b=replayed,
            event_hash_equal=replay_hash == event_hash,
            metrics_equal=True,
            differing_metrics=[],
            replayed_against="frozen-snapshot",
            current_world_digest=self._world_digest(),
            **provenance_fields(SourceKind.SYNTHETIC),
        )

    def _world_digest(self) -> str:
        return _sha(
            "live",
            str(self.reservation_version),
            *[str(len(facility.outages)) for facility in self.facilities.values()],
        )

    def compare(
        self, principal: Principal, scenario_id: str, against: str
    ) -> ScenarioComparisonResponse:
        del principal
        run_a = self.runs.get(scenario_id)
        run_b = self.runs.get(against)
        if run_a is None or run_b is None:
            raise ResourceNotFoundError()
        metrics = {
            "accepted": f"{run_a.accepted} vs {run_b.accepted}",
            "rejected": f"{run_a.rejected} vs {run_b.rejected}",
            "on_time_rate": f"{run_a.on_time_rate:.2f} vs {run_b.on_time_rate:.2f}",
        }
        verdict = (
            "policy_a_better" if run_a.on_time_rate >= run_b.on_time_rate else "policy_b_better"
        )
        return ScenarioComparisonResponse(
            scenario_id=scenario_id,
            against=against,
            run_a=run_a.run_identity,
            run_b=run_b.run_identity,
            compared_metrics=metrics,
            verdict=verdict,
            **provenance_fields(SourceKind.SYNTHETIC),
        )


# ------------------------------------------------------------------ port adapters
# Thin named objects so the composition root injects roles, not a god object.


class ReferenceOrderPort(OrderPort):
    def __init__(self, plane: InMemoryControlPlane) -> None:
        self._plane = plane

    def create(self, principal: Principal, draft: OrderDraft) -> OrderResponse:
        return self._plane.create_order(principal, draft)

    def fetch(self, principal: Principal, order_id: uuid.UUID) -> OrderResponse:
        return self._plane.fetch_order(principal, order_id)

    def list(
        self, principal: Principal, time_from: datetime, time_to: datetime
    ) -> OrderResponseList:
        return self._plane.list_orders(principal, time_from, time_to)

    def cancel(
        self, principal: Principal, order_id: uuid.UUID, draft: CancelDraft
    ) -> MissionResponse:
        return self._plane.cancel_order(principal, order_id, draft)


class ReferenceMissionPort(MissionPort, ProposalPort):
    def __init__(self, plane: InMemoryControlPlane) -> None:
        self._plane = plane

    def plan(
        self, principal: Principal, mission_id: uuid.UUID, draft: PlanDraft
    ) -> PlanVersionResponseList:
        return self._plane.plan(principal, mission_id, draft)

    def plan_versions(self, principal: Principal, mission_id: uuid.UUID) -> PlanVersionResponseList:
        return self._plane.plan_versions(principal, mission_id)

    def reserve(
        self, principal: Principal, mission_id: uuid.UUID, draft: CommitDraft
    ) -> ReservationListResponse:
        return self._plane.reserve(principal, mission_id, draft)

    def cancel(
        self, principal: Principal, mission_id: uuid.UUID, draft: CancelDraft
    ) -> MissionResponse:
        return self._plane.cancel_mission(principal, mission_id, draft)

    def reservations(self, principal: Principal, mission_id: uuid.UUID) -> ReservationListResponse:
        return self._plane.reservations(principal, mission_id)

    def decision(self, principal: Principal, mission_id: uuid.UUID) -> DecisionTraceResponse:
        return self._plane.decision(principal, mission_id)

    def uncertainty(self, principal: Principal, mission_id: uuid.UUID) -> UncertaintyStateResponse:
        return self._plane.uncertainty(principal, mission_id)

    def observations(self, principal: Principal, mission_id: uuid.UUID) -> TelemetryListResponse:
        return self._plane.observations(principal, mission_id)

    def preview_proposal(
        self,
        principal: Principal,
        mission_id: uuid.UUID,
        proposal_id: uuid.UUID,
        draft: ProposalDraft,
    ) -> MissionResponse:
        return self._plane.preview_proposal(principal, mission_id, proposal_id, draft)

    def accept_proposal(
        self,
        principal: Principal,
        mission_id: uuid.UUID,
        proposal_id: uuid.UUID,
        draft: ProposalDraft,
    ) -> MissionResponse:
        return self._plane.accept_proposal(principal, mission_id, proposal_id, draft)

    def decline_proposal(
        self,
        principal: Principal,
        mission_id: uuid.UUID,
        proposal_id: uuid.UUID,
        draft: ProposalDraft,
    ) -> MissionResponse:
        return self._plane.decline_proposal(principal, mission_id, proposal_id, draft)

    def pending(self, principal: Principal, mission_id: uuid.UUID) -> list[uuid.UUID]:
        return self._plane.proposal_ids(principal, mission_id)

    def record(self, principal: Principal, mission_id: uuid.UUID, proposal_id: uuid.UUID) -> None:
        del principal, mission_id, proposal_id


class ReferenceReservationPort(ReservationPort):
    def __init__(self, plane: InMemoryControlPlane) -> None:
        self._plane = plane

    def list(
        self, principal: Principal, time_from: datetime, time_to: datetime
    ) -> ReservationListResponse:
        return self._plane.list_reservations(principal, time_from, time_to)


class ReferenceFleetPort(FleetPort):
    def __init__(self, plane: InMemoryControlPlane) -> None:
        self._plane = plane

    def list(self, principal: Principal) -> AircraftResponseList:
        return self._plane.list_aircraft(principal)

    def fetch(self, principal: Principal, aircraft_id: uuid.UUID) -> AircraftResponse:
        return self._plane.fetch_aircraft(principal, aircraft_id)


class ReferenceFacilityPort(FacilityPort):
    def __init__(self, plane: InMemoryControlPlane) -> None:
        self._plane = plane

    def list(self, principal: Principal) -> FacilityResponseList:
        return self._plane.list_facilities(principal)

    def capacity(self, principal: Principal, facility_id: uuid.UUID) -> FacilityCapacityResponse:
        return self._plane.capacity(principal, facility_id)

    def outage(
        self, principal: Principal, facility_id: uuid.UUID, draft: OutageDraft
    ) -> FacilityResponse:
        return self._plane.report_outage(principal, facility_id, draft)


class ReferenceAuditPort(AuditPort):
    def __init__(self, plane: InMemoryControlPlane) -> None:
        self._plane = plane

    def list(
        self, principal: Principal, time_from: datetime, time_to: datetime
    ) -> AuditEventListResponse:
        return self._plane.list_audit(principal, time_from, time_to)

    def append(
        self,
        principal: Principal,
        *,
        operation_kind: str,
        resource_type: str,
        resource_id: str,
        reason_code: str,
        config_release_sha: str,
    ) -> AuditEventResponse:
        return self._plane.append_audit(
            principal,
            operation_kind=operation_kind,
            resource_type=resource_type,
            resource_id=resource_id,
            reason_code=reason_code,
            config_release_sha=config_release_sha,
        )


class ReferenceSimulationPort(SimulationPort):
    def __init__(self, plane: InMemoryControlPlane) -> None:
        self._plane = plane

    def run(self, principal: Principal, run_id: str) -> SimulationRunResponse:
        return self._plane.run(principal, run_id)

    def replay_diff(self, principal: Principal, run_id: str) -> ReplayDiffResponse:
        return self._plane.replay_diff(principal, run_id)

    def compare(
        self, principal: Principal, scenario_id: str, against: str
    ) -> ScenarioComparisonResponse:
        return self._plane.compare(principal, scenario_id, against)


class ReferenceStatusPort(StatusPort):
    def __init__(self, plane: InMemoryControlPlane) -> None:
        self._plane = plane

    def events(
        self, principal: Principal, after_sequence: int, limit: int
    ) -> list[StatusStreamEvent]:
        return self._plane.events(principal, after_sequence, limit)


class InMemoryIdempotencyLedger:
    """Client keys are per tenant and per operation; a replay returns the first answer.

    A key reused with a different request is a conflict, never a second commitment, and
    the digest comparison is what tells those two cases apart.
    """

    def __init__(self) -> None:
        self._stored: dict[tuple[str, str, str], StoredRequest] = {}

    def lookup(self, principal: Principal, operation: str, key: str) -> StoredRequest | None:
        return self._stored.get((str(principal.tenant_id), operation, key))

    def record(self, principal: Principal, operation: str, draft: StoredRequest) -> None:
        identity = (str(principal.tenant_id), operation, draft.key)
        existing = self._stored.get(identity)
        if existing is not None and existing.request_digest != draft.request_digest:
            body = json.dumps(draft.body, sort_keys=True, default=str)
            raise IdempotencyKeyReusedError(
                f"Idempotency-Key '{draft.key}' was already used for a different"
                f" request ({body[:80]})",
                existing_resource_id=existing.request_digest,
            )
        self._stored[identity] = draft
