"""Reservation ledger contracts.

``ReservationState`` preserves the Phase 0 set; ``unresolved`` exists precisely
because an uncertain outcome does not mean the airspace became empty. Resource
keys and kinds are validated against the closed allowed set (policy.py).
"""

from __future__ import annotations

import uuid
from enum import Enum

from pydantic import model_validator

from .policy import ResourceKey, ResourceKind
from .provenance import ConfigReleaseSha, ContractModel, UtcDatetime


class ReservationState(str, Enum):
    TENTATIVE = "tentative"
    COMMITTED = "committed"
    ACTIVATED = "activated"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    UNRESOLVED = "unresolved"


class Reservation(ContractModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    mission_id: uuid.UUID
    resource_key: ResourceKey
    resource_kind: ResourceKind
    valid_from: UtcDatetime
    valid_until: UtcDatetime
    state: ReservationState
    commitment_token: str
    created_at: UtcDatetime
    activated_at: UtcDatetime | None = None
    completed_at: UtcDatetime | None = None

    @model_validator(mode="after")
    def _kind_matches_key(self) -> Reservation:
        kind_from_key = self.resource_key.split(":", 1)[0]
        if kind_from_key != self.resource_kind.value:
            raise ValueError(
                "resource_kind must agree with the prefix of resource_key"
            )
        if self.valid_until <= self.valid_from:
            raise ValueError("reservation window must be increasing")
        return self


class ResourceCatalog(ContractModel):
    resource_key: ResourceKey
    kind: ResourceKind
    owner_tenant_id: uuid.UUID
    capacity: int
    status: str
    source_release: ConfigReleaseSha
