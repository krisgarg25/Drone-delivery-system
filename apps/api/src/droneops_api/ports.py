"""The ports a router may call, and the failures they may report.

``technical-build/01`` section 4 makes ``apps/api`` a composition root: request
validation, tenancy and capability enforcement, and nothing else. A route therefore
never holds a session, never builds a query and never imports
``droneops_persistence`` - ``.importlinter``'s ``api-has-no-command-path`` contract
enforces that, and the ports below are the only way in.

Each port is a ``Protocol``. The reference implementations in
:mod:`droneops_api.reference` satisfy them with the real reservation vocabulary from
``droneops_reservations``; a production wiring injects the ledger-backed adapters
(:mod:`droneops_reservations.ledger`) and the persistence-backed read models here
instead, without a single router changing.

Failures are typed so the exception handlers in
:mod:`droneops_api.composition` can render the documented status codes - including the
neutral 404 that keeps a cross-tenant read from leaking existence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol
from uuid import UUID

from droneops_api.schemas import (
    AircraftResponse,
    AircraftResponseList,
    AuditEventListResponse,
    AuditEventResponse,
    DecisionTraceResponse,
    FacilityCapacityResponse,
    FacilityResponse,
    FacilityResponseList,
    MissionResponse,
    OrderResponse,
    OrderResponseList,
    PlanVersionResponseList,
    ReplayDiffResponse,
    ReservationListResponse,
    ScenarioComparisonResponse,
    SimulationRunResponse,
    StatusStreamEvent,
    TelemetryListResponse,
    UncertaintyStateResponse,
)

# ------------------------------------------------------------------ caller identity

CAPABILITY_ORDER_WRITE = "order:write"
CAPABILITY_ORDER_READ = "order:read"
CAPABILITY_MISSION_PLAN = "mission:plan"
CAPABILITY_MISSION_COMMIT = "mission:commit"
CAPABILITY_MISSION_CANCEL = "mission:cancel"
CAPABILITY_PROPOSAL_ACCEPT = "proposal:accept"
CAPABILITY_PROPOSAL_DECLINE = "proposal:decline"
CAPABILITY_READ = "read:all"
CAPABILITY_AUDIT_READ = "audit:read"
CAPABILITY_FACILITY_OUTAGE = "facility:outage"
CAPABILITY_SIMULATION_READ = "simulation:read"

#: Every capability the control plane knows. Nothing in this list is a vehicle write,
#: and no future entry may be one: ``scripts/openapi_gate.py`` is the guard.
ALL_CAPABILITIES: frozenset[str] = frozenset(
    {
        CAPABILITY_ORDER_WRITE,
        CAPABILITY_ORDER_READ,
        CAPABILITY_MISSION_PLAN,
        CAPABILITY_MISSION_COMMIT,
        CAPABILITY_MISSION_CANCEL,
        CAPABILITY_PROPOSAL_ACCEPT,
        CAPABILITY_PROPOSAL_DECLINE,
        CAPABILITY_READ,
        CAPABILITY_AUDIT_READ,
        CAPABILITY_FACILITY_OUTAGE,
        CAPABILITY_SIMULATION_READ,
    }
)

WRITE_CAPABILITIES: frozenset[str] = frozenset(
    {
        CAPABILITY_ORDER_WRITE,
        CAPABILITY_MISSION_PLAN,
        CAPABILITY_MISSION_COMMIT,
        CAPABILITY_MISSION_CANCEL,
        CAPABILITY_PROPOSAL_ACCEPT,
        CAPABILITY_PROPOSAL_DECLINE,
        CAPABILITY_FACILITY_OUTAGE,
    }
)


@dataclass(frozen=True)
class Principal:
    """An authenticated caller, resolved once per request by the composition root."""

    tenant_id: UUID
    actor_id: str
    actor_type: str
    roles: tuple[str, ...]
    capabilities: frozenset[str]
    credential: str = "oidc"
    request_id: str = ""
    config_release_sha: str = ""

    def may(self, capability: str) -> bool:
        return capability in self.capabilities


# ------------------------------------------------------------------ commands


@dataclass(frozen=True)
class OrderDraft:
    external_ref: str
    origin_facility_id: str
    destination_facility_id: str
    payload_profile_id: str
    priority_class: str
    earliest_departure: datetime
    requested_deadline: datetime
    idempotency_key: str


@dataclass(frozen=True)
class PlanDraft:
    idempotency_key: str
    config_release_sha: str
    reason_code: str = "operator_requests_plan"


@dataclass(frozen=True)
class CommitDraft:
    idempotency_key: str
    plan_version_id: UUID
    config_release_sha: str
    reason_code: str = "operator_commits_plan"


@dataclass(frozen=True)
class CancelDraft:
    idempotency_key: str
    reason_code: str
    note: str = ""


@dataclass(frozen=True)
class OutageDraft:
    idempotency_key: str
    reason_code: str
    valid_from: datetime
    valid_until: datetime


@dataclass(frozen=True)
class ProposalDraft:
    idempotency_key: str
    reason_code: str
    note: str = ""


@dataclass(frozen=True)
class StoredRequest:
    """A previously completed write, replayed verbatim to a retrying client."""

    operation: str
    key: str
    request_digest: str
    status_code: int
    body: dict[str, object] = field(default_factory=dict)


# ------------------------------------------------------------------ failures


class ControlPlaneError(Exception):
    """Base for every domain failure a router renders as a documented status code."""

    def __init__(self, message: str, *, error_code: str = "validation_error") -> None:
        super().__init__(message)
        self.message = message
        self.error_code = error_code


class ResourceNotFoundError(ControlPlaneError):
    """Neutral absence.

    A resource that exists in another tenant and a resource that does not exist at all
    produce this same error and this same response body, so a probe cannot tell them
    apart (``technical-build/06`` section 8).
    """

    def __init__(self, message: str = "Resource not found") -> None:
        super().__init__(message, error_code="tenant_not_authorized")


class CapabilityDeniedError(ControlPlaneError):
    """The credential lacks the capability scope, whatever tenant it claims."""

    def __init__(self, capability: str) -> None:
        super().__init__(
            f"Credential is not authorised for capability '{capability}'.",
            error_code="tenant_not_authorized",
        )
        self.capability = capability


class DuplicateCommitError(ControlPlaneError):
    """A second commitment for an identity the ledger has already committed."""

    def __init__(self, message: str, existing_resource_id: str) -> None:
        super().__init__(message, error_code="duplicate_commit_conflict")
        self.existing_resource_id = existing_resource_id


class IdempotencyKeyReusedError(ControlPlaneError):
    """The same client key replayed with a different request body."""

    def __init__(self, message: str, existing_resource_id: str) -> None:
        super().__init__(message, error_code="idempotency_key_reused")
        self.existing_resource_id = existing_resource_id


class UnresolvedStateError(ControlPlaneError):
    """A workflow state the API may not invent an answer for."""

    def __init__(self, message: str) -> None:
        super().__init__(message, error_code="unresolved")


# ------------------------------------------------------------------ ports


class IdempotencyPort(Protocol):
    """Client-key ledger: a retry replays, it never re-executes a commitment."""

    def lookup(self, principal: Principal, operation: str, key: str) -> StoredRequest | None: ...

    def record(self, principal: Principal, operation: str, draft: StoredRequest) -> None: ...


class OrderPort(Protocol):
    def create(self, principal: Principal, draft: OrderDraft) -> OrderResponse: ...

    def fetch(self, principal: Principal, order_id: UUID) -> OrderResponse: ...

    def list(
        self, principal: Principal, time_from: datetime, time_to: datetime
    ) -> OrderResponseList: ...

    def cancel(
        self, principal: Principal, order_id: UUID, draft: CancelDraft
    ) -> MissionResponse: ...


class MissionPort(Protocol):
    def plan(
        self, principal: Principal, mission_id: UUID, draft: PlanDraft
    ) -> PlanVersionResponseList: ...

    def plan_versions(self, principal: Principal, mission_id: UUID) -> PlanVersionResponseList: ...

    def reserve(
        self, principal: Principal, mission_id: UUID, draft: CommitDraft
    ) -> ReservationListResponse: ...

    def cancel(
        self, principal: Principal, mission_id: UUID, draft: CancelDraft
    ) -> MissionResponse: ...

    def reservations(self, principal: Principal, mission_id: UUID) -> ReservationListResponse: ...

    def decision(self, principal: Principal, mission_id: UUID) -> DecisionTraceResponse: ...

    def uncertainty(self, principal: Principal, mission_id: UUID) -> UncertaintyStateResponse: ...

    def observations(self, principal: Principal, mission_id: UUID) -> TelemetryListResponse: ...

    def preview_proposal(
        self, principal: Principal, mission_id: UUID, proposal_id: UUID, draft: ProposalDraft
    ) -> MissionResponse: ...

    def accept_proposal(
        self, principal: Principal, mission_id: UUID, proposal_id: UUID, draft: ProposalDraft
    ) -> MissionResponse: ...

    def decline_proposal(
        self, principal: Principal, mission_id: UUID, proposal_id: UUID, draft: ProposalDraft
    ) -> MissionResponse: ...


class ReservationPort(Protocol):
    def list(
        self, principal: Principal, time_from: datetime, time_to: datetime
    ) -> ReservationListResponse: ...


class FleetPort(Protocol):
    """Read-only by construction: this port has no aircraft mutation, and neither does
    the contract. Maintenance records are written by the maintenance workflow, which is
    not reachable from the control plane at all."""

    def list(self, principal: Principal) -> AircraftResponseList: ...

    def fetch(self, principal: Principal, aircraft_id: UUID) -> AircraftResponse: ...


class FacilityPort(Protocol):
    def list(self, principal: Principal) -> FacilityResponseList: ...

    def capacity(self, principal: Principal, facility_id: UUID) -> FacilityCapacityResponse: ...

    def outage(
        self, principal: Principal, facility_id: UUID, draft: OutageDraft
    ) -> FacilityResponse: ...


class AuditPort(Protocol):
    def list(
        self, principal: Principal, time_from: datetime, time_to: datetime
    ) -> AuditEventListResponse: ...

    def append(
        self,
        principal: Principal,
        *,
        operation_kind: str,
        resource_type: str,
        resource_id: str,
        reason_code: str,
        config_release_sha: str,
    ) -> AuditEventResponse: ...


class TelemetryPort(Protocol):
    def observations(self, principal: Principal, mission_id: UUID) -> TelemetryListResponse: ...


class SimulationPort(Protocol):
    def run(self, principal: Principal, run_id: str) -> SimulationRunResponse: ...

    def replay_diff(self, principal: Principal, run_id: str) -> ReplayDiffResponse: ...

    def compare(
        self, principal: Principal, scenario_id: str, against: str
    ) -> ScenarioComparisonResponse: ...


class StatusPort(Protocol):
    """The console's event feed: a bounded read of the append-only status log."""

    def events(
        self, principal: Principal, after_sequence: int, limit: int
    ) -> list[StatusStreamEvent]: ...


class ProposalPort(Protocol):
    """Workflow proposals only: accepting one records a decision, never a flight."""

    def pending(self, principal: Principal, mission_id: UUID) -> list[UUID]: ...

    def record(self, principal: Principal, mission_id: UUID, proposal_id: UUID) -> None: ...


@dataclass(frozen=True)
class ControlPlaneServices:
    """Everything a router is allowed to reach, assembled by the composition root."""

    orders: OrderPort
    missions: MissionPort
    reservations: ReservationPort
    fleet: FleetPort
    facilities: FacilityPort
    audit: AuditPort
    simulations: SimulationPort
    status: StatusPort
    idempotency: IdempotencyPort
