"""Telemetry, domain events and the outbox (migration group ``telemetry``).

Spec section 3. Two rules are structural here rather than behavioural:

* ``(stream_type, stream_id, event_seq)`` is UNIQUE -- that is optimistic stream ordering,
  and a consumer that replays a publication collides with the constraint instead of
  producing a second projection;
* ``measurement_time`` and ``received_time`` are separate columns and
  ``aircraft_state``/``telemetry_observation`` are append-only, so a late frame cannot
  overwrite a newer accepted measurement (invariant I10).

``telemetry_observation`` is range-partitioned by ``measurement_time`` (spec section 15).
The consequence is stated plainly in the migration: a partitioned table cannot carry a
unique key that excludes the partition column, so the strict ``(tenant_id, aircraft_id,
sequence)`` uniqueness is enforced by the ingest repository's guarded upsert instead.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from geoalchemy2 import Geometry
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSTZRANGE
from sqlalchemy.orm import Mapped, mapped_column

from ..declarative import Base
from ..types import (
    TenantMixin,
    altitude_pairing_check,
    nullable_utc_instant_check,
    pg_enum,
    tenant_index,
    utc_instant_check,
)


class TelemetryObservation(Base, TenantMixin):
    __tablename__ = "telemetry_observation"
    __table_args__: tuple[Any, ...] = (
        tenant_index("telemetry_observation"),
        # I10: the receipt can be late, never earlier than the measurement it reports.
        CheckConstraint(
            "received_time >= measurement_time",
            name="receipt_is_not_before_measurement",
        ),
        utc_instant_check("measurement_time"),
        utc_instant_check("received_time"),
        CheckConstraint("sequence >= 0", name="sequence_is_not_negative"),
        CheckConstraint("length(btrim(message_type)) > 0", name="message_type_is_not_blank"),
        CheckConstraint(
            "integrity_status::text = any (array['ok','degraded','unsigned','quarantined'])",
            name="integrity_status_is_known",
        ),
        CheckConstraint("raw_hash ~ '^[0-9a-f]{64}$'", name="raw_hash_is_sha256_hex"),
        # I9: no accepted position exists without a reference, and the reference is stored
        # next to the number rather than in a side table.
        CheckConstraint(
            "altitude_reference is not null", name="altitude_reference_is_recorded"
        ),
        altitude_pairing_check("altitude_m", "altitude_reference"),
        CheckConstraint(
            "position is null or ST_SRID(position) = 4326", name="position_is_epsg4326"
        ),
        CheckConstraint(
            "altitude_m is null or altitude_reference <> 'UNSPECIFIED'",
            name="altitude_reference_is_specific",
        ),
        Index("ix_telemetry_observation_aircraft_time", "aircraft_id", "measurement_time"),
        UniqueConstraint(
            "tenant_id", "aircraft_id", "sequence", "measurement_time", name="per_aircraft_sequence"
        ),
        # Partitioned by measurement time: 90-day retention (spec section 14) becomes a
        # partition detach instead of a million-row DELETE.
        {"postgresql_partition_by": "RANGE (measurement_time)"},
    )

    # The partition key must be part of every unique constraint, hence a compound PK.
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    measurement_time: Mapped[datetime] = mapped_column(nullable=False, primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False
    )
    aircraft_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("aircraft.id", ondelete="RESTRICT"), nullable=False
    )
    mission_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("mission.id", ondelete="SET NULL")
    )
    message_type: Mapped[str] = mapped_column(String(64), nullable=False)
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    received_time: Mapped[datetime] = mapped_column(nullable=False)
    position: Mapped[Any | None] = mapped_column(Geometry("Point", srid=4326, spatial_index=False))
    altitude_m: Mapped[float | None] = mapped_column(Numeric(10, 3))
    altitude_reference: Mapped[str] = mapped_column(pg_enum("altitude_reference"), nullable=False)
    uncertainty_m: Mapped[float | None] = mapped_column(Numeric(10, 3))
    speed_ms: Mapped[float | None] = mapped_column(Numeric(8, 3))
    heading_deg: Mapped[float | None] = mapped_column(Numeric(6, 3))
    battery_pct: Mapped[float | None] = mapped_column(Numeric(6, 3))
    link_quality: Mapped[str | None] = mapped_column(pg_enum("link_quality"))
    integrity_status: Mapped[str] = mapped_column(pg_enum("integrity_status"), nullable=False)
    raw_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_adapter: Mapped[str] = mapped_column(String(200), nullable=False)
    detail_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )


class DomainEvent(Base, TenantMixin):
    """Append-only event stream. Corrections are new events, never edits (conventions section 5)."""

    __tablename__ = "domain_event"
    __table_args__: tuple[Any, ...] = (
        tenant_index("domain_event"),
        # Optimistic stream ordering, exactly as spec section 3 requires.
        UniqueConstraint("stream_type", "stream_id", "event_seq", name="stream_position"),
        CheckConstraint("event_seq >= 1", name="event_seq_starts_at_one"),
        CheckConstraint(
            "config_release_sha ~ '^sha256:[0-9a-f]{64}$'",
            name="config_release_sha_is_canonical",
        ),
        CheckConstraint("schema_version >= 1", name="schema_version_is_positive"),
        utc_instant_check("occurred_at"),
        utc_instant_check("received_at"),
        CheckConstraint(
            "received_at >= occurred_at", name="receipt_is_not_before_occurrence"
        ),
        CheckConstraint(
            "event_type = any (array["
            "'order.registered','order.cancelled','mission.created','mission.planned',"
            "'mission.replanned','reservation.committed','reservation.activated',"
            "'reservation.completed','reservation.cancelled','reservation.unresolved',"
            "'telemetry.observed','telemetry.stale','conflict.detected',"
            "'checker.verdictRecorded','aircraft.stateChanged','priority.requested',"
            "'proposal.previewed','proposal.accepted','proposal.declined',"
            "'restriction.updated','facility.outage','usage.mission_charged','config.released'"
            "])",
            name="event_type_is_in_the_catalogue",
        ),
        Index("ix_domain_event_tenant_occurred", "tenant_id", "occurred_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False
    )
    stream_type: Mapped[str] = mapped_column(pg_enum("event_stream_type"), nullable=False)
    stream_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    event_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    payload_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    occurred_at: Mapped[datetime] = mapped_column(nullable=False)
    received_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))
    actor: Mapped[str] = mapped_column(String(200), nullable=False)
    correlation_id: Mapped[str | None] = mapped_column(String(200))
    causation_id: Mapped[str | None] = mapped_column(String(200))
    config_release_sha: Mapped[str] = mapped_column(String(71), nullable=False)


class Outbox(Base, TenantMixin):
    """Transactional publication intent. The event row and this row commit together."""

    __tablename__ = "outbox"
    __table_args__: tuple[Any, ...] = (
        tenant_index("outbox"),
        # One pending row per event: a dispatcher cannot claim the same event twice.
        UniqueConstraint("event_id", name="event_id"),
        CheckConstraint("attempts >= 0", name="attempts_are_counted"),
        nullable_utc_instant_check("published_at"),
        Index("ix_outbox_unpublished", "published_at", postgresql_where=text("published_at is null")),
    )

    event_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("domain_event.id", ondelete="CASCADE"), primary_key=True
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False
    )
    published_at: Mapped[datetime | None] = mapped_column()
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    last_error: Mapped[str | None] = mapped_column(Text)
    lease_window: Mapped[Any | None] = mapped_column(TSTZRANGE)
