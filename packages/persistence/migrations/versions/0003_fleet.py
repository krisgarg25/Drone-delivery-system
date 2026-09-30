"""Fleet: aircraft_model, aircraft, aircraft_state, maintenance_event.

Revision ID: 0003_fleet
Revises: 0002_identity
Create Date: 2026-10-01

``aircraft_state`` and ``maintenance_event`` are append-only: the current position of an
aircraft is the newest accepted row, never a mutable column, which is what lets invariant
I10 hold even when the application is wrong.

``aircraft.home_facility_id`` has no foreign key here on purpose -- ``facility`` does not
exist until the ``network`` revision, which adds the constraint. That keeps one logical
change per revision instead of reordering the namespaces.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from droneops_persistence.models.fleet import (
    Aircraft,
    AircraftModel,
    AircraftState,
    MaintenanceEvent,
)
from droneops_persistence.schema_ddl import quote_identifier, secure_table

revision: str = "0003_fleet"
down_revision: str | None = "0002_identity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = (AircraftModel, Aircraft, AircraftState, MaintenanceEvent)


def upgrade() -> None:
    bind = op.get_bind()
    for model in TABLES:
        model.__table__.create(bind=bind)

    secure_table(bind, "aircraft_model")
    # Status is a state machine (conventions section 9); the row is updated, never replaced.
    secure_table(bind, "aircraft", mutable=True)
    secure_table(bind, "aircraft_state", append_only=True)
    secure_table(bind, "maintenance_event", append_only=True)

    bind.exec_driver_sql(
        "CREATE INDEX ix_aircraft_state_measurement_time "
        "ON aircraft_state (measurement_time DESC)"
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("DROP INDEX IF EXISTS ix_aircraft_state_measurement_time")
    for model in reversed(TABLES):
        bind.exec_driver_sql(
            f"DROP TABLE IF EXISTS {quote_identifier(model.__tablename__)} CASCADE"
        )
