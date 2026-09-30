"""Telemetry observation contract (data model section 9).

Telemetry is an externally-derived synthetic object, so it cannot be constructed
without provenance: ``provenance`` is a required field. ``sequence`` is monotonic
per stream, and a late or out-of-order receipt must never overwrite a newer
accepted measurement (invariant I10).
"""

from __future__ import annotations

import uuid
from enum import Enum

from pydantic import Field, model_validator

from .fleet import LinkQuality
from .geometry import Position
from .provenance import (
    ContractModel,
    Provenance,
    Sha256Hex,
    SyntheticMixin,
    UtcDatetime,
)


class UncertaintyStateKind(str, Enum):
    CLEAR = "clear"
    UNCERTAIN = "uncertain"
    LOST = "lost"
    RECONCILING = "reconciling"


class IntegrityStatus(str, Enum):
    OK = "ok"
    DEGRADED = "degraded"
    UNSIGNED = "unsigned"
    QUARANTINED = "quarantined"


class Integrity(ContractModel):
    signature_verified: bool = False
    stale: bool = False
    out_of_order: bool = False


class Kinematics(ContractModel):
    speed_ms: float = Field(ge=0.0)
    heading_deg: float = Field(ge=0.0, lt=360.0)


class Power(ContractModel):
    battery_pct: float = Field(ge=0.0, le=100.0)
    energy_wh_remaining: int = Field(ge=0)


class Link(ContractModel):
    quality: LinkQuality
    rtt_ms: int = Field(ge=0)


class TelemetryObservation(ContractModel, SyntheticMixin):
    observation_id: uuid.UUID
    tenant_id: uuid.UUID
    aircraft_id: uuid.UUID
    message_type: str = Field(min_length=1)
    sequence: int = Field(ge=0)
    measurement_time: UtcDatetime
    received_time: UtcDatetime
    position: Position
    kinematics: Kinematics
    power: Power
    link: Link
    integrity: Integrity = Integrity()
    integrity_status: IntegrityStatus = IntegrityStatus.OK
    raw_hash: Sha256Hex
    source_adapter: str
    provenance: Provenance

    @model_validator(mode="after")
    def _clock_ordering(self) -> TelemetryObservation:
        # Invariant I10: a receipt cannot predate measurement (clock jump guard).
        if self.received_time < self.measurement_time:
            raise ValueError(
                "received_time must not be earlier than measurement_time (clock jump)"
            )
        return self


class UncertaintyState(ContractModel, SyntheticMixin):
    """Monitoring projection for the Phase 4 console.

    Invariant I3: a stale or missing observation means uncertainty, never clear
    airspace. Unless the state is ``CLEAR`` the last protected volume stays
    reserved.
    """

    aircraft_id: uuid.UUID
    tenant_id: uuid.UUID
    state: UncertaintyStateKind
    last_accepted_measurement_time: UtcDatetime | None = None
    staleness_s: int = Field(ge=0)
    protected_volume_retained: bool = True

    @model_validator(mode="after")
    def _uncertainty_retains_volume(self) -> UncertaintyState:
        if self.state is not UncertaintyStateKind.CLEAR and not self.protected_volume_retained:
            raise ValueError(
                "non-clear uncertainty must retain the protected volume (I3)"
            )
        return self
