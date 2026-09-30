"""Column types and mixins that carry the invariants.

Everything in here exists so that a rule is stated once and cannot be forgotten by the
next table: ``timestamptz(6)`` and UTC (conventions section 8), a non-null ``tenant_id``
on every business row (row-level security needs it), and an altitude column that is
never alone — it always travels with an ``altitude_reference`` (invariant I9).
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import CheckConstraint, Enum as SAEnum, Index, UUID
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

# Conventions section 8 / spec section 1: every timestamp is UTC with microsecond
# precision. ``DATETIME`` would emit an unpadded ``timestamp``; the dialect type pins (6).
UTCTimestamp = TIMESTAMP(timezone=True, precision=6)

#: Every altitude column in the schema is one of these; the reference is what makes the
#: number meaningful (60 m AGL at a hub is not 60 m AMSL at a pad).
ALTITUDE_REFERENCE_VALUES: tuple[str, ...] = ("AGL", "AMSL", "GROUND", "PAD", "UNSPECIFIED")

#: Postgres enum labels created by the baseline revision. The ORM references them with
#: ``create_type=False`` so a model never tries to re-create a type another revision owns.
ENUM_TYPES: dict[str, tuple[str, ...]] = {
    "altitude_reference": ALTITUDE_REFERENCE_VALUES,
    "reservation_state": (
        "tentative",
        "committed",
        "activated",
        "completed",
        "cancelled",
        "unresolved",
    ),
    "resource_kind": ("corridor", "volume_cell", "crossing", "pad", "charger_bay", "crew"),
    "aircraft_status": (
        "registered",
        "available",
        "reserved",
        "loading",
        "flying",
        "unloading",
        "charging",
        "maintenance",
        "offline",
        "retired",
        "contingency",
    ),
    "operational_state": (
        "available",
        "reserved",
        "loading",
        "flying",
        "unloading",
        "charging",
        "maintenance",
        "offline",
        "contingency",
    ),
    "link_quality": ("good", "degraded", "lost", "unknown"),
    "emergency_state": ("none", "minor", "major", "critical"),
    "mission_status": (
        "draft",
        "eligible",
        "planning",
        "reserved_pending_commit",
        "accepted",
        "activated",
        "in_progress",
        "complete",
        "cancelled",
        "failed",
        "unresolved",
    ),
    "order_status": (
        "received",
        "validated",
        "planning",
        "accepted",
        "cancelled",
        "rejected",
        "complete",
    ),
    "priority_class": ("safety_contingency", "life_critical", "commercial_sla", "routine"),
    "handover_type": ("land_transfer", "winch", "parachute"),
    "facility_status": ("active", "outage", "closed", "survey_pending"),
    "facility_kind": ("hub", "node", "depot", "other"),
    "crew_role": ("pilot", "visual_observer", "load_handler", "duty_manager"),
    "integrity_status": ("ok", "degraded", "unsigned", "quarantined"),
    "uncertainty_state": ("clear", "uncertain", "lost", "reconciling"),
    "event_stream_type": (
        "order",
        "mission",
        "aircraft",
        "facility",
        "reservation",
        "config",
        "system",
    ),
    "audit_actor_type": ("user", "service", "checker", "break_glass"),
    "tenant_status": ("provisioning", "active", "suspended", "offboarded"),
    "account_status": ("invited", "active", "suspended", "disabled"),
    "credential_status": ("active", "rotated", "revoked", "expired"),
    "freshness": ("fresh", "aging", "stale", "unknown"),
    "sim_run_state": (
        "queued",
        "running",
        "completed",
        "failed",
        "aborted",
    ),
    "corridor_status": ("open", "restricted", "closed", "survey_pending"),
    "capacity_state": ("available", "occupied", "outage", "maintenance"),
}


#: Tables the application role may only ever add rows to. Kept beside the type helpers
#: because the baseline revision and the repositories both read it and it must be one list.
APPEND_ONLY_TABLES: tuple[str, ...] = (
    "audit_log",
    "domain_event",
    "telemetry_observation",
    "aircraft_state",
    "decision_record",
    "maintenance_event",
)


def pg_enum(name: str) -> SAEnum:
    """A native Postgres enum that is assumed to already exist.

    ``create_type=False`` is the important flag: migrations create these labels in the
    baseline revision, so an ORM ``create_all`` or a later revision must never emit a
    second ``CREATE TYPE`` and silently diverge from the migration-owned label set.
    """

    values = ENUM_TYPES[name]
    return SAEnum(*values, name=name, create_type=False, native_enum=True)


def utc_instant_check(column: str) -> CheckConstraint:
    """Pin the column to a UTC reading.

    ``timestamptz`` stores an instant, so this is not storage validation: it fails the
    write when the *session* is not UTC. That is the point. A connection opened under
    ``Asia/Kolkata`` would otherwise read ``extract(timezone ...)`` as 19830 and every
    insert would be refused -- the check is what makes "UTC everywhere" (conventions
    section 8) a database rule instead of a connection-string convention. The baseline
    revision therefore also pins ``timezone = 'UTC'`` on the application role.
    """

    return CheckConstraint(
        f"extract(timezone from {column}) = 0", name=f"{column}_is_utc"
    )


def nullable_utc_instant_check(column: str) -> CheckConstraint:
    return CheckConstraint(
        f"{column} is null or extract(timezone from {column}) = 0",
        name=f"{column}_is_utc",
    )


def altitude_pairing_check(altitude: str, reference: str) -> CheckConstraint:
    """Invariant I9: an altitude never exists without its reference.

    A NULL altitude with a reference is legal (the object has no vertical extent); an
    altitude with no reference is not, because "60" is not a height until the datum is
    named.
    """

    return CheckConstraint(
        f"{altitude} is null or {reference} is not null",
        name=f"{altitude}_requires_reference",
    )


def range_ordering_check(range_column: str) -> CheckConstraint:
    """A reservation window must be a real, non-empty, bounded interval."""

    return CheckConstraint(
        f"not isempty({range_column}) and not lower_inf({range_column}) "
        f"and not upper_inf({range_column})",
        name=f"{range_column}_is_bounded_and_non_empty",
    )


class TenantMixin:
    """Every business row is tenant-scoped; row-level security is only possible because of it."""

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
        comment="Owning tenant. Row-level security compares it to current_setting('app.tenant_id').",
    )


def tenant_index(table_name: str) -> Index:
    """The tenant_id index every business table needs, spelled once.

    It is a helper rather than a mixin ``__table_args__`` because a mixin that owns
    ``__table_args__`` silently replaces the subclass's own constraints, and every table
    here has real constraints to declare.
    """

    return Index(f"ix_{table_name}_tenant_id", "tenant_id")


__all__ = [
    "ALTITUDE_REFERENCE_VALUES",
    "APPEND_ONLY_TABLES",
    "ENUM_TYPES",
    "TenantMixin",
    "UTCTimestamp",
    "altitude_pairing_check",
    "nullable_utc_instant_check",
    "pg_enum",
    "range_ordering_check",
    "tenant_index",
    "utc_instant_check",
]
