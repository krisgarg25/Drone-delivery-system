"""Reservations: the ledger and its exclusion constraint.

Revision ID: 0007_reservations
Revises: 0006_scheduling
Create Date: 2026-10-01

The constraint below is the literal from ``11-simulation-routing-engine.md`` section 6,
issued as written rather than generated, so the physical constraint name and predicate are
auditable against the spec text. It is the last atomic guard against double-booking a
resource window; the 4D swept-volume, merge and uncertainty reasoning stays in the planner
and the independent checker (conventions section 5).

Only ``committed`` and ``activated`` are inside the predicate. ``tentative`` holds nothing
yet; ``cancelled`` and ``completed`` release the window. ``unresolved`` is deliberately
*outside* the protected set but never *released*: an uncertain outcome does not mean the
airspace became empty, and the ledger must reconcile it rather than let someone else take
the slot. That is a repository rule with a test, not a constraint the predicate can express.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from droneops_persistence.models.reservations import Reservation, ResourceCatalog
from droneops_persistence.schema_ddl import (
    drop_index,
    quote_identifier,
    secure_table,
    spatial_index,
)

revision: str = "0007_reservations"
down_revision: str | None = "0006_scheduling"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SPEC_EXCLUSION_DDL = """
ALTER TABLE reservation
  ADD CONSTRAINT no_committed_overlap
  EXCLUDE USING gist (
    resource_key WITH =,
    valid_during WITH &&
  )
  WHERE (state IN ('committed', 'activated'));
"""

TABLES = (Reservation, ResourceCatalog)


def upgrade() -> None:
    bind = op.get_bind()
    for model in TABLES:
        model.__table__.create(bind=bind)

    bind.exec_driver_sql(SPEC_EXCLUSION_DDL)
    spatial_index(bind, "ix_reservation_tenant_window", "reservation", "valid_during")

    secure_table(bind, "reservation", mutable=True)
    secure_table(bind, "resource_catalog", mutable=True)


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("ALTER TABLE reservation DROP CONSTRAINT IF EXISTS no_committed_overlap")
    drop_index(bind, "ix_reservation_tenant_window")
    for model in reversed(TABLES):
        bind.exec_driver_sql(
            f"DROP TABLE IF EXISTS {quote_identifier(model.__tablename__)} CASCADE"
        )
