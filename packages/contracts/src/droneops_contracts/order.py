"""Order intake contracts.

Priority classes are a controlled enum. The Phase 0 ``SimulatedOrderRequest`` /
``SimulatedOrderResponse`` shapes are preserved verbatim so existing API callers
keep working, while the full ``order`` entity from the data model lives beside
them.
"""

from __future__ import annotations

import uuid
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from .geometry import AltitudeReference
from .provenance import ContractModel, Sha256Hex, UtcDatetime


class PriorityClass(str, Enum):
    SAFETY_CONTINGENCY = "safety_contingency"
    LIFE_CRITICAL = "life_critical"
    COMMERCIAL_SLA = "commercial_sla"
    ROUTINE = "routine"


class OrderStatus(str, Enum):
    RECEIVED = "received"
    VALIDATED = "validated"
    PLANNING = "planning"
    ACCEPTED = "accepted"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    COMPLETE = "complete"


class Order(ContractModel):
    """The ``order`` entity: a tenant-scoped delivery request."""

    id: uuid.UUID
    tenant_id: uuid.UUID
    external_ref_hash: Sha256Hex
    origin_facility_id: str = Field(min_length=1)
    destination_facility_id: str = Field(min_length=1)
    payload_profile_id: str = Field(min_length=1)
    priority_class: PriorityClass
    earliest_departure: UtcDatetime
    requested_deadline: UtcDatetime
    status: OrderStatus
    idempotency_key: str = Field(min_length=1)

    @model_validator(mode="after")
    def _deadline_after_departure(self) -> Order:
        if self.requested_deadline <= self.earliest_departure:
            raise ValueError(
                "requested_deadline must be after earliest_departure "
                "(impossible-deadline guard)"
            )
        return self


class SimulatedOrderRequest(BaseModel):
    """Phase 0 request DTO kept mutable and permissive for the existing API."""

    schema_version: Literal[1] = 1
    origin_facility_id: str = Field(min_length=1)
    destination_facility_id: str = Field(min_length=1)
    priority_class: PriorityClass = PriorityClass.ROUTINE
    altitude_reference: AltitudeReference = AltitudeReference.AGL
    idempotency_key: str | None = None

    @model_validator(mode="after")
    def _altitude_reference_must_be_specified(self) -> SimulatedOrderRequest:
        # Invariant I9: even a request must not carry an UNSPECIFIED reference.
        if self.altitude_reference is AltitudeReference.UNSPECIFIED:
            raise ValueError("altitude_reference must not be UNSPECIFIED (I9)")
        return self


class SimulatedOrderResponse(BaseModel):
    mission_id: uuid.UUID
    status: MissionStatus
    phase: str
    authorized_for_flight: bool
    source_kind: str
    notes: str


# Imported late to break the order <-> mission reference cycle; PriorityClass is
# already defined above, so mission.py can import it from here.
from .mission import MissionStatus  # noqa: E402

SimulatedOrderResponse.model_rebuild()
