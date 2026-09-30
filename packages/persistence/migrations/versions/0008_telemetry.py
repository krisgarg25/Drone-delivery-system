"""Telemetry: partitioned observations, the event stream and the outbox.

Revision ID: 0008_telemetry
Revises: 0007_reservations
Create Date: 2026-10-01

``telemetry_observation`` is range-partitioned by ``measurement_time`` (spec section 15)
so that the 90-day retention rule (spec section 14) is a partition detach, not a
million-row DELETE that fights the ingest path for locks.

The cost of partitioning is stated in the model and here rather than hidden: a partitioned
table cannot carry a unique constraint that excludes its partition key, so
``uq_telemetry_observation_tenant_id_aircraft_id_sequence_measurement_time`` is *per
instant*, not per stream. The strict "one row per (tenant, aircraft, sequence)" rule is
therefore enforced by ``PostgresTelemetryIngest``, which refuses an insert whose sequence
already exists for the aircraft and never UPDATEs a newer measurement (invariant I10). The
database still guarantees the rest: ``received_time >= measurement_time`` and the append-only
trigger that makes an overwrite physically impossible.

``domain_event`` keeps the spec's ``(stream_type, stream_id, event_seq)`` uniqueness, which
*is* a real database guarantee because that table is not partitioned.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from droneops_persistence.models.telemetry import (
    DomainEvent,
    Outbox,
    TelemetryObservation,
)
from droneops_persistence.schema_ddl import (
    drop_index,
    quote_identifier,
    quote_literal,
    secure_table,
    spatial_index,
)

revision: str = "0008_telemetry"
down_revision: str | None = "0007_reservations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = (TelemetryObservation, DomainEvent, Outbox)

#: Months of partitions materialised at migration time. Older and newer data lands in the
#: DEFAULT partition instead of failing the insert, so retention never becomes an outage.
PARTITION_START = (2026, 7)
PARTITION_COUNT = 18


def _partition_names() -> list[tuple[str, str, str]]:
    year, month = PARTITION_START
    out: list[tuple[str, str, str]] = []
    for _ in range(PARTITION_COUNT):
        nxt_year, nxt_month = (year + 1, 1) if month == 12 else (year, month + 1)
        out.append(
            (
                f"telemetry_observation_{year}{month:02d}",
                f"{year}-{month:02d}-01",
                f"{nxt_year}-{nxt_month:02d}-01",
            )
        )
        year, month = nxt_year, nxt_month
    return out


def upgrade() -> None:
    bind = op.get_bind()
    for model in TABLES:
        model.__table__.create(bind=bind)

    for name, lower, upper in _partition_names():
        bind.exec_driver_sql(
            f"CREATE TABLE {quote_identifier(name)} PARTITION OF telemetry_observation "
            f"FOR VALUES FROM ({quote_literal(lower)}) TO ({quote_literal(upper)})"
        )
    bind.exec_driver_sql(
        "CREATE TABLE telemetry_observation_default PARTITION OF telemetry_observation DEFAULT"
    )
    spatial_index(bind, "ix_telemetry_observation_position", "telemetry_observation", "position")

    secure_table(bind, "telemetry_observation", append_only=True)
    secure_table(bind, "domain_event", append_only=True)
    # The outbox is the one mutable row in this group: publishing updates it, and a
    # dispatcher may delete a claimed row after retention. Never the event itself.
    secure_table(bind, "outbox", mutable=True, allow_delete=True)


def downgrade() -> None:
    bind = op.get_bind()
    drop_index(bind, "ix_telemetry_observation_position")
    for name, _lower, _upper in _partition_names():
        bind.exec_driver_sql(f"DROP TABLE IF EXISTS {quote_identifier(name)}")
    bind.exec_driver_sql("DROP TABLE IF EXISTS telemetry_observation_default")
    for model in reversed(TABLES):
        bind.exec_driver_sql(
            f"DROP TABLE IF EXISTS {quote_identifier(model.__tablename__)} CASCADE"
        )
