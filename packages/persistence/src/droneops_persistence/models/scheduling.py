"""Mission, plan version and decision record (migration group ``scheduling``).

Spec section 2 "Orders and missions" and section 8. ``decision_record`` is what makes the
system explain itself: eligible and rejected candidates are stored, not just the winner,
and every plan and decision carries ``config_release_sha`` (conventions section 8).

``UNIQUE (tenant_id, order_id)`` on mission is the second half of idempotency: even if an
application bug produced a second INSERT for the same order, the ledger cannot hold two
missions for one request.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ..declarative import Base
from ..types import TenantMixin, nullable_utc_instant_check, pg_enum, tenant_index, utc_instant_check


class Mission(Base, TenantMixin):
    __tablename__ = "mission"
    __table_args__: tuple[Any, ...] = (
        tenant_index("mission"),
        UniqueConstraint("tenant_id", "order_id", name="tenant_id_order_id"),
        nullable_utc_instant_check("activated_at"),
        nullable_utc_instant_check("completed_at"),
        CheckConstraint(
            "completed_at is null or activated_at is null or completed_at >= activated_at",
            name="completion_is_not_before_activation",
        ),
        CheckConstraint("length(btrim(policy_version)) > 0", name="policy_version_is_not_blank"),
        Index("ix_mission_tenant_status", "tenant_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    order_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("order.id", ondelete="RESTRICT"), nullable=False
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False
    )
    aircraft_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("aircraft.id", ondelete="RESTRICT")
    )
    status: Mapped[str] = mapped_column(
        pg_enum("mission_status"), nullable=False, server_default=text("'draft'")
    )
    plan_version_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("plan_version.id", ondelete="SET NULL", use_alter=True),
    )
    priority_class: Mapped[str] = mapped_column(pg_enum("priority_class"), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(200), nullable=False)
    activated_at: Mapped[datetime | None] = mapped_column()
    completed_at: Mapped[datetime | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))


class PlanVersion(Base, TenantMixin):
    """An immutable candidate plan. A new plan is a new row; ``superseded_by`` points forward."""

    __tablename__ = "plan_version"
    __table_args__: tuple[Any, ...] = (
        tenant_index("plan_version"),
        CheckConstraint(
            "route_hash ~ '^[0-9a-f]{64}$'", name="route_hash_is_sha256_hex"
        ),
        CheckConstraint(
            "config_release_sha ~ '^sha256:[0-9a-f]{64}$'",
            name="config_release_sha_is_canonical",
        ),
        # The margin is exposed as a number, never collapsed to a boolean (spec section 8).
        CheckConstraint(
            "energy_margin_wh is null or energy_margin_wh >= -100000",
            name="energy_margin_is_a_recorded_number",
        ),
        CheckConstraint(
            "superseded_by is null or superseded_by <> id", name="plan_does_not_supersede_itself"
        ),
        utc_instant_check("created_at"),
        Index("ix_plan_version_mission_created", "mission_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    mission_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("mission.id", ondelete="RESTRICT"), nullable=False
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False
    )
    route_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    profile_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    energy_margin_wh: Mapped[int | None] = mapped_column(Integer)
    config_release_sha: Mapped[str] = mapped_column(String(71), nullable=False)
    # Simulated plans are never authoritative (conventions section 3).
    authoritative: Mapped[bool] = mapped_column(
        nullable=False, server_default=text("false"), default=False
    )
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("plan_version.id", ondelete="SET NULL")
    )


class DecisionRecord(Base, TenantMixin):
    """Why this plan and not the others. Append-only; a correction is a new record."""

    __tablename__ = "decision_record"
    __table_args__: tuple[Any, ...] = (
        tenant_index("decision_record"),
        CheckConstraint(
            "input_hash ~ '^[0-9a-f]{64}$'", name="input_hash_is_sha256_hex"
        ),
        CheckConstraint(
            "config_release_sha ~ '^sha256:[0-9a-f]{64}$'",
            name="config_release_sha_is_canonical",
        ),
        CheckConstraint(
            "coalesce(array_length(reason_codes, 1), 0) > 0", name="reason_codes_are_not_empty"
        ),
        CheckConstraint("length(btrim(deciding_actor)) > 0", name="deciding_actor_is_not_blank"),
        utc_instant_check("created_at"),
        Index("ix_decision_record_mission", "mission_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    mission_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("mission.id", ondelete="RESTRICT"), nullable=False
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False
    )
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    eligible_candidates_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    rejected_candidates_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    selected_plan_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("plan_version.id", ondelete="RESTRICT")
    )
    reason_codes: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    deciding_actor: Mapped[str] = mapped_column(String(200), nullable=False)
    config_release_sha: Mapped[str] = mapped_column(String(71), nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))
