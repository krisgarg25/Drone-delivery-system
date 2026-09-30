"""Facility, pad, charger-bay and crew contracts.

``handover_type`` is a controlled enum whose only permitted value in this build
is ``LAND_TRANSFER``; ``WINCH`` and ``PARACHUTE`` exist as values for foreign
references but are rejected by validation.
"""

from __future__ import annotations

import uuid
from enum import Enum

from pydantic import model_validator

from .geometry import TimeWindow
from .policy import OperatorRole
from .provenance import ContractModel, Provenance, Sha256Hex, UtcDatetime


class HandoverType(str, Enum):
    LAND_TRANSFER = "land_transfer"
    WINCH = "winch"
    PARACHUTE = "parachute"


# The Indian government medical delivery template mandates land-and-transfer only;
# WINCH and PARACHUTE are never valid for this build, so they are rejected here.
_FORBIDDEN_HANDOVER = {HandoverType.WINCH, HandoverType.PARACHUTE}


class FacilityStatus(str, Enum):
    ACTIVE = "active"
    OUTAGE = "outage"
    CLOSED = "closed"
    SURVEY_PENDING = "survey_pending"


class Facility(ContractModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    name: str
    kind: str
    address_ref: str
    boundary_hash: Sha256Hex | None = None
    status: FacilityStatus
    source: str
    source_updated_at: UtcDatetime
    provenance: Provenance


class Pad(ContractModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    facility_id: uuid.UUID
    name: str
    capacity: int
    approach_profile_id: str
    handover_type: HandoverType = HandoverType.LAND_TRANSFER
    status: FacilityStatus

    @model_validator(mode="after")
    def _reject_banned_handover(self) -> Pad:
        if self.handover_type in _FORBIDDEN_HANDOVER:
            raise ValueError(
                f"handover_type {self.handover_type.value!r} is banned for this build"
            )
        return self


class ChargerBay(ContractModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    facility_id: uuid.UUID
    pad_id: uuid.UUID
    connector_type: str
    recharge_minutes: int
    status: FacilityStatus


class CrewAssignment(ContractModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    facility_id: uuid.UUID
    crew_id: uuid.UUID
    duty_window: TimeWindow
    role: OperatorRole
    status: str
