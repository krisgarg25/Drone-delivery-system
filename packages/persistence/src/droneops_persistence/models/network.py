"""Facility, pad, charger and crew tables (migration group ``network``).

Spec section 2 "Facilities". ``handover_type`` is a controlled enum whose only Phase 1
default is ``land_transfer`` (the Indian government medical template); ``winch`` and
``parachute`` exist as values for foreign reference material and are refused as a default
by a check constraint, not by a comment.

Geometry is always stored with an explicit CRS: the SRID is pinned by a check constraint
because a mis-labelled Chandigarh point is silently somewhere else on Earth, and the
GiST indexes are created by the migration rather than inferred.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from geoalchemy2 import Geometry
from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
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
)


class Facility(Base, TenantMixin):
    __tablename__ = "facility"
    __table_args__: tuple[Any, ...] = (
        tenant_index("facility"),
        CheckConstraint("length(btrim(name)) > 0", name="name_is_not_blank"),
        CheckConstraint("length(btrim(source)) > 0", name="source_is_not_blank"),
        CheckConstraint(
            "boundary_geom is null or ST_SRID(boundary_geom) = 4326",
            name="boundary_is_epsg4326",
        ),
        CheckConstraint(
            "centre is null or ST_SRID(centre) = 4326", name="centre_is_epsg4326"
        ),
        altitude_pairing_check("elevation_m", "elevation_reference"),
        nullable_utc_instant_check("source_updated_at"),
        # The GiST indexes are created by the network revision: spec section 15 requires
        # spatial index creation to be explicit and versioned, not inferred from a model.
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(pg_enum("facility_kind"), nullable=False)
    status: Mapped[str] = mapped_column(
        pg_enum("facility_status"), nullable=False, server_default=text("'active'")
    )
    address_ref: Mapped[str | None] = mapped_column(String(200))
    boundary_geom: Mapped[Any | None] = mapped_column(
        Geometry("Polygon", srid=4326, spatial_index=False)
    )
    centre: Mapped[Any | None] = mapped_column(
        Geometry("Point", srid=4326, spatial_index=False)
    )
    elevation_m: Mapped[float | None] = mapped_column(Numeric(10, 3))
    elevation_reference: Mapped[str | None] = mapped_column(pg_enum("altitude_reference"))
    opening_hours_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    source: Mapped[str] = mapped_column(String(200), nullable=False)
    source_updated_at: Mapped[datetime | None] = mapped_column()
    # Synthetic by default and hard to strip: a surveyed boundary has to say so (conventions section 3).
    authoritative: Mapped[bool] = mapped_column(
        nullable=False, server_default=text("false"), default=False
    )
    requires_survey: Mapped[bool] = mapped_column(
        nullable=False, server_default=text("true"), default=True
    )


class Pad(Base, TenantMixin):
    __tablename__ = "pad"
    __table_args__: tuple[Any, ...] = (
        tenant_index("pad"),
        CheckConstraint("length(btrim(name)) > 0", name="name_is_not_blank"),
        # I6: pad capacity is a physical ceiling, so a pad that can hold nothing is a
        # configuration error, not a closed pad (that is what ``status`` is for).
        CheckConstraint("capacity >= 1", name="capacity_is_at_least_one"),
        CheckConstraint("capacity <= 64", name="capacity_is_plausible"),
        CheckConstraint(
            "handover_type::text = any (array['land_transfer','winch','parachute'])",
            name="handover_type_is_known",
        ),
        CheckConstraint(
            "location is null or ST_SRID(location) = 4326", name="location_is_epsg4326"
        ),
        altitude_pairing_check("elevation_m", "elevation_reference"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    facility_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("facility.id", ondelete="RESTRICT"), nullable=False
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    capacity: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    approach_profile_id: Mapped[str | None] = mapped_column(String(200))
    handover_type: Mapped[str] = mapped_column(
        pg_enum("handover_type"), nullable=False, server_default=text("'land_transfer'")
    )
    status: Mapped[str] = mapped_column(
        pg_enum("capacity_state"), nullable=False, server_default=text("'available'")
    )
    location: Mapped[Any | None] = mapped_column(
        Geometry("Point", srid=4326, spatial_index=False)
    )
    elevation_m: Mapped[float | None] = mapped_column(Numeric(10, 3))
    elevation_reference: Mapped[str | None] = mapped_column(pg_enum("altitude_reference"))


class ChargerBay(Base, TenantMixin):
    __tablename__ = "charger_bay"
    __table_args__: tuple[Any, ...] = (
        tenant_index("charger_bay"),
        CheckConstraint("recharge_minutes > 0", name="recharge_minutes_is_positive"),
        CheckConstraint(
            "connector_type = any (array['ccs1','ccs2','chaoji','proprietary','other'])",
            name="connector_type_is_known",
        ),
        CheckConstraint("power_w > 0", name="power_w_is_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    facility_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("facility.id", ondelete="RESTRICT"), nullable=False
    )
    pad_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("pad.id", ondelete="RESTRICT")
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False
    )
    connector_type: Mapped[str] = mapped_column(String(32), nullable=False)
    recharge_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    power_w: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("3000"))
    status: Mapped[str] = mapped_column(
        pg_enum("capacity_state"), nullable=False, server_default=text("'available'")
    )


class CrewAssignment(Base, TenantMixin):
    """A duty window is a tstzrange so it can participate in the same exclusion machinery."""

    __tablename__ = "crew_assignment"
    __table_args__: tuple[Any, ...] = (
        tenant_index("crew_assignment"),
        CheckConstraint("not isempty(duty_window)", name="duty_window_is_not_empty"),
        CheckConstraint(
            "not lower_inf(duty_window) and not upper_inf(duty_window)",
            name="duty_window_is_bounded",
        ),
        CheckConstraint(
            "extract(timezone from lower(duty_window)) = 0", name="duty_window_start_is_utc"
        ),
        CheckConstraint(
            "extract(timezone from upper(duty_window)) = 0", name="duty_window_end_is_utc"
        ),
        CheckConstraint("length(btrim(crew_id)) > 0", name="crew_id_is_not_blank"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False
    )
    facility_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("facility.id", ondelete="RESTRICT"), nullable=False
    )
    crew_id: Mapped[str] = mapped_column(String(200), nullable=False)
    role: Mapped[str] = mapped_column(pg_enum("crew_role"), nullable=False)
    status: Mapped[str] = mapped_column(
        pg_enum("capacity_state"), nullable=False, server_default=text("'available'")
    )
    duty_window: Mapped[Any] = mapped_column(TSTZRANGE, nullable=False)
