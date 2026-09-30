"""Shared versioned contracts for the DroneOps operator platform.

This is the single source of truth for cross-module types. It is pure: no
database, HTTP or framework dependency (enforced by ``.importlinter``). Every
public type is re-exported from the package root so existing importers such as
``from droneops_contracts import MissionStatus`` keep working.
"""

from __future__ import annotations

from .decision import (
    DecisionRecord,
    DecisionTrace,
    DecisionTraceStep,
    EligibleCandidate,
    RejectedCandidate,
)
from .energy import EnergyEstimate, EnergyReservePolicy
from .errors import ConflictResponse, ErrorCode, ErrorDetail, ErrorResponse
from .events import (
    EVENT_TYPES,
    EventEnvelope,
    EventStreamType,
    EventType,
)
from .facilities import (
    ChargerBay,
    CrewAssignment,
    Facility,
    FacilityStatus,
    HandoverType,
    Pad,
)
from .fleet import (
    Aircraft,
    AircraftModel,
    AircraftState,
    AircraftStatus,
    EmergencyState,
    LinkQuality,
    MaintenanceEvent,
    OperationalState,
)
from .geometry import AltitudeReference, Position, TimeWindow
from .mission import Mission, MissionStatus, PlanVersion
from .order import (
    Order,
    OrderStatus,
    PriorityClass,
    SimulatedOrderRequest,
    SimulatedOrderResponse,
)
from .policy import (
    ALLOWED_RESOURCE_KINDS,
    OperatorRole,
    ProtectedVolumePolicy,
    ResourceKey,
    ResourceKind,
    validate_resource_key,
)
from .provenance import (
    ContractModel,
    Freshness,
    JsonValue,
    Provenance,
    SyntheticMixin,
    ensure_utc,
)
from .reservation import Reservation, ReservationState, ResourceCatalog
from .route import CoordinateTimeProfile, PlanPayload, RoutePoint
from .scenario import (
    ReplayDiff,
    RunIdentity,
    ScenarioBundle,
    ScenarioComparison,
    ScenarioFault,
    ScenarioUnits,
)
from .telemetry import (
    Integrity,
    IntegrityStatus,
    Kinematics,
    Link,
    Power,
    TelemetryObservation,
    UncertaintyState,
    UncertaintyStateKind,
)
from .usage import AuditLogEntry, Outbox, UsageCharge

__all__ = [
    # provenance / primitives
    "ContractModel",
    "SyntheticMixin",
    "Provenance",
    "Freshness",
    "JsonValue",
    "ensure_utc",
    # geometry
    "AltitudeReference",
    "Position",
    "TimeWindow",
    # policy
    "ResourceKind",
    "ResourceKey",
    "ALLOWED_RESOURCE_KINDS",
    "validate_resource_key",
    "ProtectedVolumePolicy",
    "OperatorRole",
    # order
    "PriorityClass",
    "OrderStatus",
    "Order",
    "SimulatedOrderRequest",
    "SimulatedOrderResponse",
    # mission
    "MissionStatus",
    "Mission",
    "PlanVersion",
    # reservation
    "ReservationState",
    "Reservation",
    "ResourceCatalog",
    # fleet
    "OperationalState",
    "AircraftStatus",
    "LinkQuality",
    "EmergencyState",
    "AircraftModel",
    "Aircraft",
    "AircraftState",
    "MaintenanceEvent",
    # facilities
    "HandoverType",
    "FacilityStatus",
    "Facility",
    "Pad",
    "ChargerBay",
    "CrewAssignment",
    # route / energy
    "RoutePoint",
    "CoordinateTimeProfile",
    "PlanPayload",
    "EnergyEstimate",
    "EnergyReservePolicy",
    # telemetry
    "IntegrityStatus",
    "Integrity",
    "Kinematics",
    "Power",
    "Link",
    "TelemetryObservation",
    "UncertaintyStateKind",
    "UncertaintyState",
    # decision
    "EligibleCandidate",
    "RejectedCandidate",
    "DecisionRecord",
    "DecisionTraceStep",
    "DecisionTrace",
    # usage / audit
    "Outbox",
    "AuditLogEntry",
    "UsageCharge",
    # events
    "EVENT_TYPES",
    "EventType",
    "EventStreamType",
    "EventEnvelope",
    # errors
    "ErrorCode",
    "ErrorDetail",
    "ErrorResponse",
    "ConflictResponse",
    # scenario
    "ScenarioUnits",
    "ScenarioFault",
    "ScenarioBundle",
    "RunIdentity",
    "ReplayDiff",
    "ScenarioComparison",
]
