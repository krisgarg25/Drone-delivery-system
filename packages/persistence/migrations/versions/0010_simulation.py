"""Simulation: the Phase 1 world-and-run tables.

Revision ID: 0010_simulation
Revises: 0009_audit
Create Date: 2026-10-01

``05-implementation-phases.md`` Phase 1 adds scenario, sim_run, world_snapshot,
config_release, corridor and run_metric. ``sim_run`` holds the eight reproducibility
identifiers (conventions section 4) and is constrained to a single solver worker, because a
run whose output depends on thread count is not evidence of anything.

Corridors are synthetic by a check constraint, not by a comment: Phase 1 has no obstacle
survey, so a row claiming ``synthetic = false`` would be a false statement about the world
rather than an invalid value.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from droneops_persistence.models.simulation import (
    ConfigRelease,
    Corridor,
    RunMetric,
    Scenario,
    SimRun,
    WorldSnapshot,
)
from droneops_persistence.schema_ddl import (
    drop_index,
    quote_identifier,
    secure_table,
    spatial_index,
)

revision: str = "0010_simulation"
down_revision: str | None = "0009_audit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Referential order: config_release and scenario first, then the run that names both, then
# the per-run children, then the corridor layer which references facility only.
TABLES = (ConfigRelease, Scenario, SimRun, WorldSnapshot, Corridor, RunMetric)


def upgrade() -> None:
    bind = op.get_bind()
    for model in TABLES:
        model.__table__.create(bind=bind)

    spatial_index(bind, "ix_corridor_centerline", "corridor", "centerline")

    secure_table(bind, "config_release")
    secure_table(bind, "scenario")
    # A run transitions state; its identity fields are not updatable because the row that
    # records what produced an output must never be edited after the fact.
    secure_table(bind, "sim_run", mutable=True)
    secure_table(bind, "world_snapshot", append_only=True)
    secure_table(bind, "corridor", mutable=True)
    secure_table(bind, "run_metric", append_only=True)


def downgrade() -> None:
    bind = op.get_bind()
    drop_index(bind, "ix_corridor_centerline")
    for model in reversed(TABLES):
        bind.exec_driver_sql(
            f"DROP TABLE IF EXISTS {quote_identifier(model.__tablename__)} CASCADE"
        )
