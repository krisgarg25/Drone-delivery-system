"""Order intake (migration group ``commercial``).

Spec section 2 "Orders and missions" plus section 11. The idempotency guarantee is a
database property, not a service convention: ``UNIQUE (tenant_id, idempotency_key)`` makes
a duplicate insert impossible, so a retry after a network failure either returns the
original receipt or is refused (invariant I2). ``external_ref_hash`` keeps the partner's
own identifier out of the row (spec section 14).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ..declarative import Base
from ..types import TenantMixin, pg_enum, tenant_index, utc_instant_check


class Order(Base, TenantMixin):
    __tablename__ = "order"
    __table_args__: tuple[Any, ...] = (
        tenant_index("order"),
        # I2: one partner request, one order. A second INSERT for the same key in the
        # same tenant cannot reach a second mission or a second charge.
        UniqueConstraint("tenant_id", "idempotency_key", name="tenant_id_idempotency_key"),
        CheckConstraint(
            "length(btrim(idempotency_key)) > 0", name="idempotency_key_is_not_blank"
        ),
        CheckConstraint(
            "external_ref_hash ~ '^[0-9a-f]{64}$'", name="external_ref_hash_is_sha256_hex"
        ),
        CheckConstraint(
            "requested_deadline > earliest_departure",
            name="deadline_is_after_earliest_departure",
        ),
        utc_instant_check("earliest_departure"),
        utc_instant_check("requested_deadline"),
        utc_instant_check("created_at"),
        CheckConstraint(
            "cancelled_at is null or cancelled_at >= created_at",
            name="cancellation_is_not_before_creation",
        ),
        CheckConstraint(
            "destination_facility_id <> origin_facility_id",
            name="origin_and_destination_differ",
        ),
        Index("ix_order_tenant_status_deadline", "tenant_id", "status", "requested_deadline"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False
    )
    external_ref_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    origin_facility_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("facility.id", ondelete="RESTRICT"), nullable=False
    )
    destination_facility_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("facility.id", ondelete="RESTRICT"), nullable=False
    )
    payload_profile_id: Mapped[str] = mapped_column(String(200), nullable=False)
    priority_class: Mapped[str] = mapped_column(pg_enum("priority_class"), nullable=False)
    earliest_departure: Mapped[datetime] = mapped_column(nullable=False)
    requested_deadline: Mapped[datetime] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(
        pg_enum("order_status"), nullable=False, server_default=text("'received'")
    )
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    config_release_sha: Mapped[str] = mapped_column(String(71), nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))
    cancelled_at: Mapped[datetime | None] = mapped_column()
    payload_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
