"""Repository ports: the only database-shaped things the rest of the system may touch.

`docs/conventions.md` section 7 gives `platform` the ports and `persistence` the models,
and `.importlinter` forbids `droneops_api`, `droneops_reservations`, `droneops_contracts`
and `droneops_core_domain` from importing `droneops_persistence` or `sqlalchemy`. This
module is therefore deliberately stdlib-only: every value here is a frozen dataclass of
primitives, and every method is a `Protocol` that `droneops_persistence` implements.

Consequences that are load-bearing, not stylistic:

* a port method can never return an ORM object, so no session leaks upward;
* the tenant and configuration-release identity travels in :class:`TenantContext`, which
  is what row-level security binds to (`set_config('app.tenant_id', ...)`);
* :class:`CommitConflict` is a *result*, not a crash: the reservation ledger refusing a
  double booking is the expected outcome of a race, and callers must be able to tell
  "someone else committed first" from "the database is broken".
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable

JsonMapping = Mapping[str, object]


class PortError(RuntimeError):
    """Base class for port-level failures that carry no ORM or driver detail."""


class CommitConflict(PortError):
    """The ledger refused a commit.

    ``reason`` distinguishes the refusal causes that the planner must treat differently:
    ``overlap`` is a genuine contention loss (retry from candidate generation), while
    ``unknown_resource`` or ``policy`` are configuration faults that must not be retried.
    """

    def __init__(self, reason: str, resource_key: str, detail: str = "") -> None:
        super().__init__(f"{reason} for {resource_key}: {detail}" if detail else f"{reason} for {resource_key}")
        self.reason = reason
        self.resource_key = resource_key


class CrossTenantAccess(PortError):
    """Row-level security refused the read or write. Never translated into a 403."""


class AppendOnlyViolation(PortError):
    """Something tried to mutate a committed operational record in place."""


@dataclass(frozen=True)
class TenantContext:
    """Who is acting, in which tenant, against which configuration release."""

    tenant_id: uuid.UUID
    actor: str
    config_release_sha: str
    request_id: str = ""
    correlation_id: str = ""
    causation_id: str = ""


@dataclass(frozen=True)
class ResourceWindow:
    """One protected-resource time window the planner is asking to commit."""

    resource_key: str
    resource_kind: str
    valid_from: datetime
    valid_until: datetime


@dataclass(frozen=True)
class CommitRequest:
    """An atomic commit of the full resource set for one plan version.

    ``commitment_token`` is unique per resource set and candidate version, so a replayed
    request after a network failure resolves to the same ledger state instead of a second
    commitment (conventions section 5, spec section 11).
    """

    tenant_id: uuid.UUID
    mission_id: uuid.UUID
    plan_version_id: uuid.UUID
    commitment_token: str
    windows: tuple[ResourceWindow, ...]
    config_release_sha: str
    actor: str = "scheduler"
    request_id: str = ""


@dataclass(frozen=True)
class CommitResult:
    mission_id: uuid.UUID
    commitment_token: str
    reservation_ids: tuple[uuid.UUID, ...]
    committed: bool
    decision_record_id: uuid.UUID
    event_id: uuid.UUID


@dataclass(frozen=True)
class ReservationView:
    id: uuid.UUID
    tenant_id: uuid.UUID
    mission_id: uuid.UUID
    resource_key: str
    resource_kind: str
    state: str
    valid_from: datetime
    valid_until: datetime
    commitment_token: str


@dataclass(frozen=True)
class OrderDraft:
    tenant_id: uuid.UUID
    idempotency_key: str
    origin_facility_id: uuid.UUID
    destination_facility_id: uuid.UUID
    payload_profile_id: str
    priority_class: str
    earliest_departure: datetime
    requested_deadline: datetime
    external_ref_hash: str
    config_release_sha: str


@dataclass(frozen=True)
class OrderReceipt:
    order_id: uuid.UUID
    mission_id: uuid.UUID
    status: str
    duplicate: bool


@dataclass(frozen=True)
class EventDraft:
    tenant_id: uuid.UUID
    stream_type: str
    stream_id: uuid.UUID
    event_seq: int
    event_type: str
    schema_version: int
    payload: JsonMapping
    occurred_at: datetime
    actor: str
    config_release_sha: str
    correlation_id: str = ""
    causation_id: str = ""


@dataclass(frozen=True)
class AuditEntry:
    tenant_id: uuid.UUID
    occurred_at: datetime
    actor_type: str
    actor_id: str
    action: str
    resource_type: str
    resource_id: str
    request_id: str
    reason_code: str
    config_release_sha: str
    before_sha256: str | None = None
    after_sha256: str | None = None
    ip_hash: str = ""
    device_hash: str = ""


@dataclass(frozen=True)
class AuditRecord:
    id: int
    tenant_id: uuid.UUID
    prev_hash: str
    record_hash: str
    entry: AuditEntry


@dataclass(frozen=True)
class ObservationDraft:
    tenant_id: uuid.UUID
    aircraft_id: uuid.UUID
    message_type: str
    sequence: int
    measurement_time: datetime
    received_time: datetime
    latitude: float
    longitude: float
    altitude_m: float
    altitude_reference: str
    uncertainty_m: float
    speed_ms: float
    heading_deg: float
    battery_pct: float
    energy_wh_remaining: float
    link_quality: str
    integrity_status: str
    raw_hash: str
    source_adapter: str


@dataclass(frozen=True)
class ObservationResult:
    observation_id: uuid.UUID
    accepted: bool
    out_of_order: bool
    stale: bool
    superseding_id: uuid.UUID | None = None


@runtime_checkable
class ReservationLedger(Protocol):
    """The only writer of committed airspace. The planner proposes; this decides."""

    def commit(self, context: TenantContext, request: CommitRequest) -> CommitResult:
        """Atomically commit the whole resource set, or leave no row behind."""

    def cancel(self, context: TenantContext, mission_id: uuid.UUID, reason_code: str) -> int:
        """Cancel outstanding reservations; returns the number transitioned."""

    def committed_overlapping(
        self,
        context: TenantContext,
        resource_key: str,
        valid_from: datetime,
        valid_until: datetime,
    ) -> Sequence[ReservationView]:
        """Read-only view of what already holds the resource."""


@runtime_checkable
class OrderIntake(Protocol):
    """Idempotent order and mission creation."""

    def register(self, context: TenantContext, draft: OrderDraft) -> OrderReceipt:
        """Insert or return the identical receipt for the same idempotency key."""


@runtime_checkable
class EventStore(Protocol):
    """Append-only stream events plus their outbox rows, in the caller's transaction."""

    def append(self, context: TenantContext, draft: EventDraft) -> uuid.UUID: ...

    def pending_outbox(self, context: TenantContext, limit: int = 100) -> Sequence[uuid.UUID]: ...


@runtime_checkable
class AuditChain(Protocol):
    """Hash-linked audit log. The implementing role has INSERT and SELECT only."""

    def append(self, context: TenantContext, entry: AuditEntry) -> AuditRecord: ...

    def head(self, context: TenantContext) -> AuditRecord | None: ...

    def verify_chain(self, context: TenantContext) -> Sequence[str]:
        """Return the descriptions of every broken link; empty means intact."""


@runtime_checkable
class TelemetryIngest(Protocol):
    """Read-only observation sink that never lets a late frame overwrite a newer one."""

    def record(self, context: TenantContext, draft: ObservationDraft) -> ObservationResult: ...


@runtime_checkable
class UnitOfWork(Protocol):
    """One transaction. Everything in conventions section 5 lives inside it."""

    def __enter__(self) -> object: ...

    def __exit__(self, *exc_info: object) -> bool: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


@dataclass(frozen=True)
class Ports:
    """The injected bundle an application process needs; nothing here is a model class."""

    ledger: ReservationLedger
    orders: OrderIntake
    events: EventStore
    audit: AuditChain
    telemetry: TelemetryIngest
    unit_of_work: type[UnitOfWork]
    extras: Mapping[str, object] = field(default_factory=dict)


__all__ = [
    "AppendOnlyViolation",
    "AuditChain",
    "AuditEntry",
    "AuditRecord",
    "CommitConflict",
    "CommitRequest",
    "CommitResult",
    "CrossTenantAccess",
    "EventDraft",
    "EventStore",
    "JsonMapping",
    "ObservationDraft",
    "ObservationResult",
    "OrderDraft",
    "OrderIntake",
    "OrderReceipt",
    "PortError",
    "Ports",
    "ReservationLedger",
    "ReservationView",
    "ResourceWindow",
    "TelemetryIngest",
    "TenantContext",
    "UnitOfWork",
]
