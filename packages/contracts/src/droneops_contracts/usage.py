"""Usage, outbox and audit-chain contracts (data model sections 3 and 4).

The audit log is append-only: the application role may INSERT and SELECT, never
UPDATE or DELETE. ``record_hash`` chains deterministically over the canonical
payload and ``prev_hash``. Monetary amounts use integer minor units.
"""

from __future__ import annotations

import uuid

from pydantic import Field

from .provenance import (
    ConfigReleaseSha,
    ContractModel,
    Sha256Hex,
    UtcDatetime,
)


class Outbox(ContractModel):
    event_id: uuid.UUID
    tenant_id: uuid.UUID
    published_at: UtcDatetime | None = None
    attempts: int = Field(default=0, ge=0)
    last_error: str | None = None


class AuditLogEntry(ContractModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    occurred_at: UtcDatetime
    actor_type: str
    actor_id: str
    action: str
    resource_type: str
    resource_id: str
    before_sha256: Sha256Hex | None = None
    after_sha256: Sha256Hex | None = None
    request_id: str
    ip_hash: Sha256Hex | None = None
    device_hash: Sha256Hex | None = None
    reason_code: str
    config_release_sha: ConfigReleaseSha
    prev_hash: Sha256Hex | None = None
    record_hash: Sha256Hex


class UsageCharge(ContractModel):
    """The payload for ``usage.mission_charged``. Money is integer minor units."""

    id: uuid.UUID
    tenant_id: uuid.UUID
    mission_id: uuid.UUID
    charge_kind: str
    amount_minor_units: int = Field(ge=0)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    idempotency_key: str = Field(min_length=1)
    charged_at: UtcDatetime
