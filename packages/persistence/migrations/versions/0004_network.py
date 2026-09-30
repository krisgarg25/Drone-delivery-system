"""Network: facility, pad, charger_bay, crew_assignment, plus the fleet-to-site link.

Revision ID: 0004_network
Revises: 0003_fleet
Create Date: 2026-10-01

Every GiST index in this revision is created explicitly (spec section 15): a spatial index
that a model inferred silently is an index that disappears on the next refactor, and a
reservation query that degrades from a GiST scan to a full table scan is a correctness
problem dressed as a performance one.

The ``crew_assignment.duty_window`` GiST index exists so crew can participate in the same
exclusion machinery as airspace; capacity 1 per crew member per window is enforced here.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from droneops_persistence.models.network import (
    ChargerBay,
    CrewAssignment,
    Facility,
    Pad,
)
from droneops_persistence.schema_ddl import (
    drop_index,
    quote_identifier,
    secure_table,
    spatial_index,
)

revision: str = "0004_network"
down_revision: str | None = "0003_fleet"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = (Facility, Pad, ChargerBay, CrewAssignment)

SPATIAL_INDEXES: tuple[tuple[str, str, str], ...] = (
    ("ix_facility_boundary_geom", "facility", "boundary_geom"),
    ("ix_facility_centre", "facility", "centre"),
    ("ix_pad_location", "pad", "location"),
    ("ix_crew_assignment_duty_window", "crew_assignment", "duty_window"),
)


def upgrade() -> None:
    bind = op.get_bind()
    for model in TABLES:
        model.__table__.create(bind=bind)

    for name, table, column in SPATIAL_INDEXES:
        spatial_index(bind, name, table, column)

    # The link that fleet could not declare because facility did not exist yet.
    bind.exec_driver_sql(
        "ALTER TABLE aircraft ADD CONSTRAINT fk_aircraft_home_facility_id_facility "
        "FOREIGN KEY (home_facility_id) REFERENCES facility (id) ON DELETE RESTRICT NOT VALID"
    )
    bind.exec_driver_sql(
        "ALTER TABLE aircraft VALIDATE CONSTRAINT fk_aircraft_home_facility_id_facility"
    )

    secure_table(bind, "facility", mutable=True)
    secure_table(bind, "pad", mutable=True)
    secure_table(bind, "charger_bay", mutable=True)
    secure_table(bind, "crew_assignment", mutable=True)

    # I6 support: one crew member cannot be rostered into two overlapping duties.
    bind.exec_driver_sql(
        """
ALTER TABLE crew_assignment ADD CONSTRAINT no_double_booked_crew
    EXCLUDE USING gist (
        crew_id WITH =,
        duty_window WITH &&
    )
    WHERE (status IN ('available', 'occupied'))
"""
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql(
        "ALTER TABLE crew_assignment DROP CONSTRAINT IF EXISTS no_double_booked_crew"
    )
    bind.exec_driver_sql(
        "ALTER TABLE aircraft DROP CONSTRAINT IF EXISTS fk_aircraft_home_facility_id_facility"
    )
    for name, _table, _column in reversed(SPATIAL_INDEXES):
        drop_index(bind, name)
    for model in reversed(TABLES):
        bind.exec_driver_sql(
            f"DROP TABLE IF EXISTS {quote_identifier(model.__tablename__)} CASCADE"
        )
