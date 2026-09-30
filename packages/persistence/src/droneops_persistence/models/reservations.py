"""Reservation ledger (migration group ``reservations``).

The exclusion constraint here is the literal from ``11-simulation-routing-engine.md``
section 6 and is created verbatim by the migration:

```sql
ALTER TABLE reservation
  ADD CONSTRAINT no_committed_overlap
  EXCLUDE USING gist (
    resource_key WITH =,
    valid_during WITH &&
  )
  WHERE (state IN ('committed', 'activated'));
```

That is the atomic, last-line guard against double-booking a resource window. It is *not*
the 4D model: transition uncertainty, merge volumes and the swept protected volume stay
with the planner and the independent checker (conventions section 5 -- the planner
proposes, only this ledger commits, and the database is the final guard).

``tentative``, ``cancelled``, ``completed`` and ``unresolved`` are outside the index
predicate on purpose: a tentative row holds nothing yet, and ``unresolved`` is deliberately
*inside* neither the exclusion nor release -- an uncertain outcome does not mean the
airspace became empty, so an unresolved reservation must be reconciled, not reused.
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
from sqlalchemy.dialects.postgresql import TSTZRANGE
from sqlalchemy.orm import Mapped, mapped_column

from ..declarative import Base
from ..types import TenantMixin, pg_enum, range_ordering_check, tenant_index

COMMITTING_STATES = ("committed", "activated")


class Reservation(Base, TenantMixin):
    __tablename__ = "reservation"
    __table_args__: tuple[Any, ...] = (
        tenant_index("reservation"),
        # The exclusion is NOT declared here on purpose: revision 0007_reservations issues
        # the literal DDL from `11-simulation-routing-engine.md` section 6, so the physical
        # constraint name and predicate are exactly the spec's and cannot drift from it.
        # A test asserts the constraint definition the database reports.
        UniqueConstraint("tenant_id", "commitment_token", "resource_key", name="per_resource_token"),
        range_ordering_check("valid_during"),
        CheckConstraint(
            "activated_at is null or activated_at >= created_at",
            name="activation_is_not_before_creation",
        ),
        CheckConstraint(
            "completed_at is null or activated_at is null or completed_at >= activated_at",
            name="completion_is_not_before_activation",
        ),
        CheckConstraint(
            "position(resource_kind::text || ':' in resource_key) = 1",
            name="kind_matches_key_prefix",
        ),
        CheckConstraint(
            "(state = 'committed' and activated_at is null) or state <> 'committed'",
            name="activation_timestamp_is_consistent",
        ),
        CheckConstraint(
            "lower(valid_during) < upper(valid_during)", name="window_is_forward"
        ),
        Index("ix_reservation_resource_key", "resource_key"),
        Index("ix_reservation_mission", "mission_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False
    )
    mission_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("mission.id", ondelete="RESTRICT"), nullable=False
    )
    resource_key: Mapped[str] = mapped_column(String(512), nullable=False)
    resource_kind: Mapped[str] = mapped_column(pg_enum("resource_kind"), nullable=False)
    valid_during: Mapped[Any] = mapped_column(TSTZRANGE, nullable=False)
    state: Mapped[str] = mapped_column(
        pg_enum("reservation_state"), nullable=False, server_default=text("'tentative'")
    )
    commitment_token: Mapped[str] = mapped_column(String(200), nullable=False)
    config_release_sha: Mapped[str] = mapped_column(String(71), nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))
    activated_at: Mapped[datetime | None] = mapped_column()
    completed_at: Mapped[datetime | None] = mapped_column()
    cancelled_reason_code: Mapped[str | None] = mapped_column(String(64))


class ResourceCatalog(Base, TenantMixin):
    """The register of things that can be reserved.

    ``tenant_id`` is the spec's ``owner_tenant_id``: the field is renamed so the rule
    "every business row carries a non-null tenant_id" (conventions section 8) holds
    uniformly and one RLS policy shape covers the whole schema.
    """

    __tablename__ = "resource_catalog"
    __table_args__: tuple[Any, ...] = (
        tenant_index("resource_catalog"),
        CheckConstraint("capacity >= 1", name="capacity_is_at_least_one"),
        CheckConstraint(
            "position(kind::text || ':' in resource_key) = 1",
            name="kind_matches_key_prefix",
        ),
        CheckConstraint(
            "source_release ~ '^sha256:[0-9a-f]{64}$'", name="source_release_is_canonical"
        ),
    )

    resource_key: Mapped[str] = mapped_column(String(512), primary_key=True)
    kind: Mapped[str] = mapped_column(pg_enum("resource_kind"), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False
    )
    capacity: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    status: Mapped[str] = mapped_column(
        pg_enum("capacity_state"), nullable=False, server_default=text("'available'")
    )
    source_release: Mapped[str] = mapped_column(String(71), nullable=False)
    label: Mapped[str | None] = mapped_column(Text)
