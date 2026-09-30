"""SQLAlchemy models, grouped by the migration namespaces in
``02-repository-layout.md`` section 9.

No ORM model is shared across bounded modules except through contracts and repository
ports, so an API or domain package never sees these classes.
"""

from __future__ import annotations

from .audit import GENESIS_HASH, AuditLog
from .commercial import Order
from .fleet import Aircraft, AircraftModel, AircraftState, MaintenanceEvent
from .identity import PHASE1_ROLES, ApiCredential, Role, Tenant, UserAccount, UserRole
from .network import ChargerBay, CrewAssignment, Facility, Pad
from .reservations import COMMITTING_STATES, Reservation, ResourceCatalog
from .scheduling import DecisionRecord, Mission, PlanVersion
from .simulation import (
    ConfigRelease,
    Corridor,
    RunMetric,
    Scenario,
    SimRun,
    WorldSnapshot,
)
from .telemetry import DomainEvent, Outbox, TelemetryObservation

MODEL_GROUPS: dict[str, tuple[str, ...]] = {
    "identity": ("tenant", "user_account", "role", "user_role", "api_credential"),
    "fleet": ("aircraft_model", "aircraft", "aircraft_state", "maintenance_event"),
    "network": ("facility", "pad", "charger_bay", "crew_assignment"),
    "commercial": ("order",),
    "scheduling": ("mission", "plan_version", "decision_record"),
    "reservations": ("reservation", "resource_catalog"),
    "telemetry": ("telemetry_observation", "domain_event", "outbox"),
    "audit": ("audit_log",),
    "simulation": (
        "config_release",
        "scenario",
        "sim_run",
        "world_snapshot",
        "corridor",
        "run_metric",
    ),
}

__all__ = [
    "COMMITTING_STATES",
    "GENESIS_HASH",
    "MODEL_GROUPS",
    "PHASE1_ROLES",
    "Aircraft",
    "AircraftModel",
    "AircraftState",
    "ApiCredential",
    "AuditLog",
    "ChargerBay",
    "ConfigRelease",
    "Corridor",
    "CrewAssignment",
    "DecisionRecord",
    "DomainEvent",
    "Facility",
    "MaintenanceEvent",
    "Mission",
    "Order",
    "Outbox",
    "Pad",
    "PlanVersion",
    "Reservation",
    "ResourceCatalog",
    "Role",
    "RunMetric",
    "Scenario",
    "SimRun",
    "TelemetryObservation",
    "Tenant",
    "UserAccount",
    "UserRole",
    "WorldSnapshot",
]
