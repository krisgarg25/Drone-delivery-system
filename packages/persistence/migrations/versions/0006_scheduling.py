"""Scheduling: mission, plan_version, decision_record.

Revision ID: 0006_scheduling
Revises: 0005_commercial
Create Date: 2026-10-01

``UNIQUE (tenant_id, order_id)`` on mission closes the second half of idempotency: even a
bug that produced a second ``INSERT`` for one order cannot produce a second mission.

``decision_record`` is append-only, so "why this aircraft and this route" survives the
replan that supersedes it, and ``plan_version.superseded_by`` points forward instead of a
correction overwriting the old plan (spec section 1: corrections are superseding records).

The state machine itself (``planning -> activated`` is impossible) is not expressible as a
column check, so it lives in ``droneops_reservations``/``droneops_core_domain`` transitions
and is guarded here by the ``reserved_pending_commit`` label existing in the enum at all.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from droneops_persistence.models.scheduling import (
    DecisionRecord,
    Mission,
    PlanVersion,
)
from droneops_persistence.schema_ddl import quote_identifier, secure_table

revision: str = "0006_scheduling"
down_revision: str | None = "0005_commercial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Creation order matters: plan_version references mission, and mission's pointer to the
# current plan_version is a deferred (use_alter) constraint added after both exist.
TABLES = (Mission, PlanVersion, DecisionRecord)


def upgrade() -> None:
    bind = op.get_bind()
    Mission.__table__.create(bind=bind)
    PlanVersion.__table__.create(bind=bind)
    DecisionRecord.__table__.create(bind=bind)

    bind.exec_driver_sql(
        "ALTER TABLE mission ADD CONSTRAINT fk_mission_plan_version_id_plan_version "
        "FOREIGN KEY (plan_version_id) REFERENCES plan_version (id) ON DELETE SET NULL"
    )

    secure_table(bind, "mission", mutable=True)
    # A plan is never edited: a new candidate is a new row and the old one points forward.
    secure_table(bind, "plan_version", mutable=True)
    secure_table(bind, "decision_record", append_only=True)


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql(
        "ALTER TABLE mission DROP CONSTRAINT IF EXISTS fk_mission_plan_version_id_plan_version"
    )
    # mission must go before plan_version: it holds a reference to it.
    for name in ("decision_record", "mission", "plan_version"):
        bind.exec_driver_sql(f"DROP TABLE IF EXISTS {quote_identifier(name)} CASCADE")
