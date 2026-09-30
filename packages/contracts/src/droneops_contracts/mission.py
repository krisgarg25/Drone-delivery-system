"""Mission and plan-version contracts.

``MissionStatus`` preserves the Phase 0 name and value set. The mission state
machine forbids ``planning -> activated``; that transition is enforced in the
core domain, while the contract only permits the enumerated set of states.
"""

from __future__ import annotations

import uuid
from enum import Enum

from .provenance import (
    ConfigReleaseSha,
    ContractModel,
    Provenance,
    Sha256Hex,
    SyntheticMixin,
    UtcDatetime,
)


class MissionStatus(str, Enum):
    DRAFT = "draft"
    ELIGIBLE = "eligible"
    PLANNING = "planning"
    RESERVED_PENDING_COMMIT = "reserved_pending_commit"
    ACCEPTED = "accepted"
    ACTIVATED = "activated"
    IN_PROGRESS = "in_progress"
    COMPLETE = "complete"
    CANCELLED = "cancelled"
    FAILED = "failed"
    UNRESOLVED = "unresolved"


# Imported after MissionStatus so order.py's late import of MissionStatus finds it
# defined, breaking the order <-> mission reference cycle in either import order.
from .order import PriorityClass  # noqa: E402


class Mission(ContractModel, SyntheticMixin):
    id: uuid.UUID
    order_id: uuid.UUID
    tenant_id: uuid.UUID
    aircraft_id: uuid.UUID | None = None
    status: MissionStatus
    plan_version_id: uuid.UUID | None = None
    activated_at: UtcDatetime | None = None
    completed_at: UtcDatetime | None = None
    priority_class: PriorityClass
    policy_version: str


class PlanVersion(ContractModel, SyntheticMixin):
    """A committed plan candidate; every plan carries a config-release hash."""

    id: uuid.UUID
    tenant_id: uuid.UUID
    mission_id: uuid.UUID
    route_hash: Sha256Hex
    energy_margin_wh: int
    config_release_sha: ConfigReleaseSha
    created_at: UtcDatetime
    superseded_by: uuid.UUID | None = None
    provenance: Provenance
