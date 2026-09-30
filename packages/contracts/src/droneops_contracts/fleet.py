"""Fleet contracts: aircraft models, aircraft, telemetry state and maintenance.

``AircraftState`` uses the nine-value ``operational_state`` enum. Unknown values
are rejected by the enum. Positions carry an explicit altitude reference (I9).
"""

from __future__ import annotations

import uuid
from enum import Enum

from pydantic import Field

from .geometry import Position
from .provenance import ContractModel, UtcDatetime


class OperationalState(str, Enum):
    AVAILABLE = "available"
    RESERVED = "reserved"
    LOADING = "loading"
    FLYING = "flying"
    UNLOADING = "unloading"
    CHARGING = "charging"
    MAINTENANCE = "maintenance"
    OFFLINE = "offline"
    CONTINGENCY = "contingency"


class AircraftStatus(str, Enum):
    REGISTERED = "registered"
    AVAILABLE = "available"
    RESERVED = "reserved"
    LOADING = "loading"
    FLYING = "flying"
    UNLOADING = "unloading"
    CHARGING = "charging"
    MAINTENANCE = "maintenance"
    OFFLINE = "offline"
    RETIRED = "retired"
    CONTINGENCY = "contingency"


class LinkQuality(str, Enum):
    GOOD = "good"
    DEGRADED = "degraded"
    LOST = "lost"
    UNKNOWN = "unknown"


class EmergencyState(str, Enum):
    NONE = "none"
    MINOR = "minor"
    MAJOR = "major"
    CRITICAL = "critical"


class AircraftModel(ContractModel):
    """A type-catalog entry. Not tenant-scoped: it is a platform-wide model."""

    id: uuid.UUID
    name: str
    vendor: str
    type_cert_status: str
    empty_mass_g: int = Field(ge=0)
    max_payload_g: int = Field(ge=0)
    cruise_speed_ms: float = Field(ge=0.0)
    max_wind_ms: float = Field(ge=0.0)
    battery_profile_id: str
    created_at: UtcDatetime


class Aircraft(ContractModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    model_id: uuid.UUID
    registration_ref: str
    status: AircraftStatus
    home_facility_id: uuid.UUID
    last_position_id: uuid.UUID | None = None
    maintenance_due_at: UtcDatetime | None = None


class AircraftState(ContractModel):
    aircraft_id: uuid.UUID
    tenant_id: uuid.UUID
    measurement_time: UtcDatetime
    received_time: UtcDatetime
    operational_state: OperationalState
    battery_pct: float = Field(ge=0.0, le=100.0)
    energy_wh_remaining: int = Field(ge=0)
    position: Position
    uncertainty_m: float = Field(ge=0.0)
    link_quality: LinkQuality
    emergency_state: EmergencyState


class MaintenanceEvent(ContractModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    aircraft_id: uuid.UUID
    occurred_at: UtcDatetime
    event_type: str
    severity: str
    notes: str
    cleared_by: uuid.UUID | None = None
    cleared_at: UtcDatetime | None = None
