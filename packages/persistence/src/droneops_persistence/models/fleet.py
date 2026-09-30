"""Fleet tables (migration group ``fleet``).

Spec section 2 "Aircraft and fleet". ``aircraft_state`` is append-only history: an
observation is never updated in place, which is the structural half of invariant I10 --
``measurement_time`` and ``received_time`` are separate columns and a late frame cannot
silently become the current position.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ..declarative import Base
from ..types import (
    altitude_pairing_check,
    nullable_utc_instant_check,
    pg_enum,
    tenant_index,
    utc_instant_check,
    TenantMixin,
)


class AircraftModel(Base, TenantMixin):
    __tablename__ = "aircraft_model"
    __table_args__: tuple[Any, ...] = (
        tenant_index("aircraft_model"),
        CheckConstraint("length(btrim(name)) > 0", name="name_is_not_blank"),
        CheckConstraint("empty_mass_g > 0", name="empty_mass_is_positive"),
        CheckConstraint("max_payload_g >= 0", name="max_payload_is_not_negative"),
        CheckConstraint("cruise_speed_ms > 0", name="cruise_speed_is_positive"),
        CheckConstraint("max_wind_ms > 0", name="max_wind_is_positive"),
        # A model whose certification is claimed as real is a documentation forgery in
        # Phase 1: every profile here is synthetic (conventions section 3).
        CheckConstraint(
            "type_cert_status = any (array['none','claimed','verified','not_applicable'])",
            name="type_cert_status_is_known",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    vendor: Mapped[str] = mapped_column(String(200), nullable=False)
    type_cert_status: Mapped[str] = mapped_column(String(32), nullable=False)
    empty_mass_g: Mapped[int] = mapped_column(BigInteger, nullable=False)
    max_payload_g: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cruise_speed_ms: Mapped[float] = mapped_column(Numeric(8, 3), nullable=False)
    max_wind_ms: Mapped[float] = mapped_column(Numeric(8, 3), nullable=False)
    battery_profile_id: Mapped[str] = mapped_column(String(200), nullable=False)
    # Every margin the model implies is an experiment parameter read from a versioned
    # release, never a literal in code (conventions section 3).
    config_release_sha: Mapped[str] = mapped_column(String(71), nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))


class Aircraft(Base, TenantMixin):
    __tablename__ = "aircraft"
    __table_args__: tuple[Any, ...] = (
        tenant_index("aircraft"),
        CheckConstraint("length(btrim(registration_ref)) > 0", name="registration_is_not_blank"),
        CheckConstraint(
            "maintenance_due_at is null or extract(timezone from maintenance_due_at) = 0",
            name="maintenance_due_at_is_utc",
        ),
        Index("ix_aircraft_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False
    )
    model_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("aircraft_model.id", ondelete="RESTRICT"), nullable=False
    )
    registration_ref: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(pg_enum("aircraft_status"), nullable=False)
    home_facility_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
    # A soft pointer to the newest accepted observation. No FK on purpose: telemetry is
    # created after fleet, and the pointer is a projection, not an ownership relation.
    last_position_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    maintenance_due_at: Mapped[datetime | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))


class AircraftState(Base, TenantMixin):
    """Append-only fleet state history. There is no "current" row to overwrite."""

    __tablename__ = "aircraft_state"
    __table_args__: tuple[Any, ...] = (
        tenant_index("aircraft_state"),
        altitude_pairing_check("altitude_m", "altitude_reference"),
        utc_instant_check("measurement_time"),
        utc_instant_check("received_time"),
        CheckConstraint(
            "received_time >= measurement_time",
            name="receipt_is_not_before_measurement",
        ),
        CheckConstraint("battery_pct >= 0 and battery_pct <= 100", name="battery_pct_is_a_percentage"),
        CheckConstraint("energy_wh_remaining >= 0", name="energy_wh_remaining_is_not_negative"),
        CheckConstraint("uncertainty_m >= 0", name="uncertainty_is_not_negative"),
        # I3 support: a lost link must arrive with an uncertainty figure. "We cannot see it"
        # is recorded as a volume of doubt, never as an empty sky.
        CheckConstraint(
            "link_quality <> 'lost' or uncertainty_m is not null",
            name="lost_link_records_uncertainty",
        ),
        Index("ix_aircraft_state_aircraft_measurement", "aircraft_id", "measurement_time"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    aircraft_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("aircraft.id", ondelete="RESTRICT"), nullable=False
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False
    )
    measurement_time: Mapped[datetime] = mapped_column(nullable=False)
    received_time: Mapped[datetime] = mapped_column(nullable=False)
    operational_state: Mapped[str] = mapped_column(pg_enum("operational_state"), nullable=False)
    battery_pct: Mapped[float] = mapped_column(Numeric(6, 3), nullable=False)
    energy_wh_remaining: Mapped[int] = mapped_column(Integer, nullable=False)
    position_lat: Mapped[float | None] = mapped_column(Numeric(9, 6))
    position_lon: Mapped[float | None] = mapped_column(Numeric(9, 6))
    altitude_m: Mapped[float | None] = mapped_column(Numeric(10, 3))
    altitude_reference: Mapped[str | None] = mapped_column(pg_enum("altitude_reference"))
    uncertainty_m: Mapped[float | None] = mapped_column(Numeric(10, 3))
    link_quality: Mapped[str] = mapped_column(
        pg_enum("link_quality"), nullable=False, server_default=text("'unknown'")
    )
    emergency_state: Mapped[str] = mapped_column(
        pg_enum("emergency_state"), nullable=False, server_default=text("'none'")
    )
    config_release_sha: Mapped[str] = mapped_column(String(71), nullable=False)


class MaintenanceEvent(Base, TenantMixin):
    __tablename__ = "maintenance_event"
    __table_args__: tuple[Any, ...] = (
        tenant_index("maintenance_event"),
        utc_instant_check("occurred_at"),
        nullable_utc_instant_check("cleared_at"),
        CheckConstraint(
            "cleared_at is null or cleared_at >= occurred_at",
            name="clearing_is_not_before_the_event",
        ),
        # An uncleared event is a live restriction on eligibility (I5), so the pairing of
        # cleared_by/cleared_at is enforced rather than trusted.
        CheckConstraint(
            "(cleared_at is null and cleared_by is null) or (cleared_at is not null and cleared_by is not null)",
            name="clearance_is_complete",
        ),
        CheckConstraint(
            "event_type = any (array['scheduled','fault','inspection','part_replacement','software','other'])",
            name="event_type_is_known",
        ),
        CheckConstraint("severity = any (array['info','minor','major','critical'])", name="severity_is_known"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    aircraft_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("aircraft.id", ondelete="RESTRICT"), nullable=False
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False
    )
    occurred_at: Mapped[datetime] = mapped_column(nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[str] = mapped_column(String(32), nullable=False)
    notes: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    cleared_by: Mapped[str | None] = mapped_column(String(200))
    cleared_at: Mapped[datetime | None] = mapped_column()
    detail_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
