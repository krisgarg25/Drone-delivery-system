"""Phase 1 simulation tables (migration group ``simulation``).

``05-implementation-phases.md`` adds ``scenario``, ``sim_run``, ``world_snapshot``,
``config_release``, ``corridor`` and ``run_metric``. ``sim_run`` stores the eight
identifiers that make a run re-computable (conventions section 4): bundle hash, engine
version, scenario hash, policy version, config-release hash, seed, Python version and
machine architecture. A run that cannot state those is not a proof.

``run_metric`` stores the *formula and denominator* alongside the value, because spec
section 9 requires every metric to be defined rather than implied -- including the ones
that count rejected work.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from geoalchemy2 import Geometry
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSTZRANGE
from sqlalchemy.orm import Mapped, mapped_column

from ..declarative import Base
from ..types import (
    TenantMixin,
    altitude_pairing_check,
    pg_enum,
    tenant_index,
    utc_instant_check,
)

SHA256_HEX = "^[0-9a-f]{64}$"
CONFIG_SHA = "^sha256:[0-9a-f]{64}$"


class ConfigRelease(Base, TenantMixin):
    """The versioned configuration a plan or decision was taken against."""

    __tablename__ = "config_release"
    __table_args__: tuple[Any, ...] = (
        tenant_index("config_release"),
        CheckConstraint(
            f"config_release_sha ~ '{SHA256_HEX}'", name="sha_is_hex"
        ),
        CheckConstraint(
            "protected_volume_json ->> 'altitude_reference' is not null",
            name="protected_volume_declares_altitude_reference",
        ),
        CheckConstraint(
            "protected_volume_json ->> 'illustrative' = 'true'",
            name="margins_are_marked_illustrative",
        ),
        utc_instant_check("released_at"),
        UniqueConstraint("tenant_id", "config_release_sha", "label", name="tenant_sha_label"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False
    )
    config_release_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(200), nullable=False)
    protected_volume_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    released_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))


class Scenario(Base, TenantMixin):
    __tablename__ = "scenario"
    __table_args__: tuple[Any, ...] = (
        tenant_index("scenario"),
        UniqueConstraint("tenant_id", "scenario_id", "schema_version", name="tenant_scenario_version"),
        CheckConstraint("length(btrim(scenario_id)) > 0", name="scenario_id_is_not_blank"),
        # Conventions section 3: synthetic by default and hard to strip. A scenario may
        # only claim authority by also naming a source, a retrieval date, a licence and an
        # approved use, which the check below makes a requirement rather than a review note.
        CheckConstraint(
            "authoritative = false or ("
            "source is not null and source_retrieved_at is not null "
            "and licence_ref is not null and approved_use is not null)",
            name="authoritative_requires_provenance",
        ),
        CheckConstraint("seed >= 0", name="seed_is_recorded"),
        CheckConstraint(
            f"bundle_sha256 ~ '{SHA256_HEX}'", name="bundle_sha256_is_hex"
        ),
        CheckConstraint(
            "altitude_reference::text = any (array['AGL','AMSL','GROUND','PAD','UNSPECIFIED'])",
            name="altitude_reference_is_known",
        ),
        utc_instant_check("created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False
    )
    scenario_id: Mapped[str] = mapped_column(String(200), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    authoritative: Mapped[bool] = mapped_column(
        nullable=False, server_default=text("false"), default=False
    )
    altitude_reference: Mapped[str] = mapped_column(
        pg_enum("altitude_reference"), nullable=False, server_default=text("'AGL'")
    )
    time_zone: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default=text("'UTC'")
    )
    seed: Mapped[int] = mapped_column(Integer, nullable=False)
    bundle_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    source: Mapped[str | None] = mapped_column(Text)
    source_retrieved_at: Mapped[datetime | None] = mapped_column()
    licence_ref: Mapped[str | None] = mapped_column(String(200))
    approved_use: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))


class SimRun(Base, TenantMixin):
    """A deterministic run, identified by its eight reproducibility fields."""

    __tablename__ = "sim_run"
    __table_args__: tuple[Any, ...] = (
        tenant_index("sim_run"),
        UniqueConstraint("tenant_id", "run_key", name="tenant_run_key"),
        CheckConstraint(
            f"bundle_sha256 ~ '{SHA256_HEX}' and scenario_sha256 ~ '{SHA256_HEX}' "
            f"and config_release_sha256 ~ '{SHA256_HEX}' and event_vector_sha256 ~ '{SHA256_HEX}'",
            name="run_identifiers_are_sha256",
        ),
        CheckConstraint("seed >= 0", name="seed_is_recorded"),
        CheckConstraint("length(btrim(engine_version)) > 0", name="engine_version_is_recorded"),
        CheckConstraint("length(btrim(python_version)) > 0", name="python_version_is_recorded"),
        CheckConstraint(
            "length(btrim(machine_architecture)) > 0", name="machine_architecture_is_recorded"
        ),
        # The kernel is single-threaded and uses no wall clock (conventions section 4).
        CheckConstraint("solver_worker_count = 1", name="solver_worker_count_is_one"),
        CheckConstraint("started_at is null or extract(timezone from started_at) = 0", name="started_at_is_utc"),
        CheckConstraint("finished_at is null or extract(timezone from finished_at) = 0", name="finished_at_is_utc"),
        CheckConstraint(
            "finished_at is null or started_at is null or finished_at >= started_at",
            name="finish_is_after_start",
        ),
        CheckConstraint("authoritative = false", name="simulation_is_never_authoritative"),
        Index("ix_sim_run_scenario_started", "scenario_id", "started_at"),
        CheckConstraint("horizon_end_us > horizon_start_us", name="horizon_is_forward"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False
    )
    scenario_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("scenario.id", ondelete="RESTRICT"), nullable=False
    )
    config_release_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("config_release.id", ondelete="RESTRICT")
    )
    run_key: Mapped[str] = mapped_column(String(200), nullable=False)
    state: Mapped[str] = mapped_column(
        pg_enum("sim_run_state"), nullable=False, server_default=text("'queued'")
    )
    bundle_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    engine_version: Mapped[str] = mapped_column(String(200), nullable=False)
    scenario_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(200), nullable=False)
    config_release_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    seed: Mapped[int] = mapped_column(Integer, nullable=False)
    python_version: Mapped[str] = mapped_column(String(64), nullable=False)
    machine_architecture: Mapped[str] = mapped_column(String(64), nullable=False)
    solver_worker_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    # Integer microseconds, never a float wall clock (conventions section 4).
    horizon_start_us: Mapped[int] = mapped_column(BigInteger, nullable=False)
    horizon_end_us: Mapped[int] = mapped_column(BigInteger, nullable=False)
    event_vector_sha256: Mapped[str | None] = mapped_column(String(64))
    authoritative: Mapped[bool] = mapped_column(
        nullable=False, server_default=text("false"), default=False
    )
    started_at: Mapped[datetime | None] = mapped_column()
    finished_at: Mapped[datetime | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))


class WorldSnapshot(Base, TenantMixin):
    """The frozen world a commit was planned against (conventions section 5 step 2)."""

    __tablename__ = "world_snapshot"
    __table_args__: tuple[Any, ...] = (
        tenant_index("world_snapshot"),
        UniqueConstraint("run_id", "sim_time_us", name="one_snapshot_per_sim_instant"),
        CheckConstraint("snapshot_sha256 ~ '^[0-9a-f]{64}$'", name="snapshot_sha256_is_hex"),
        CheckConstraint("sim_time_us >= 0", name="sim_time_is_not_negative"),
        utc_instant_check("captured_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("sim_run.id", ondelete="CASCADE"), nullable=False
    )
    sim_time_us: Mapped[int] = mapped_column(BigInteger, nullable=False)
    snapshot_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    reservation_version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    world_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    captured_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))


class Corridor(Base, TenantMixin):
    """A synthetic lane. Nothing here is surveyed, and the row says so."""

    __tablename__ = "corridor"
    __table_args__: tuple[Any, ...] = (
        tenant_index("corridor"),
        UniqueConstraint("tenant_id", "corridor_key", name="tenant_corridor_key"),
        CheckConstraint(
            "centerline is null or ST_SRID(centerline) = 4326", name="centerline_is_epsg4326"
        ),
        CheckConstraint("length(btrim(corridor_key)) > 0", name="corridor_key_is_not_blank"),
        CheckConstraint(
            "corridor_key like 'corridor:%'", name="corridor_key_is_a_resource_key"
        ),
        CheckConstraint(
            "ceiling_altitude_m is null or floor_altitude_m is null "
            "or ceiling_altitude_m >= floor_altitude_m",
            name="ceiling_is_not_below_floor",
        ),
        CheckConstraint(
            "floor_altitude_m is not null or ceiling_altitude_m is not null",
            name="altitude_band_is_declared",
        ),
        altitude_pairing_check("floor_altitude_m", "floor_altitude_reference"),
        altitude_pairing_check("ceiling_altitude_m", "ceiling_altitude_reference"),
        CheckConstraint(
            "floor_altitude_reference = ceiling_altitude_reference",
            name="band_uses_one_reference",
        ),
        # Chandigarh OSM carries effectively no vertical data: any obstacle height here is
        # generated and must stay flagged (conventions section 3).
        CheckConstraint("synthetic = true", name="phase1_corridors_are_synthetic"),
        CheckConstraint("requires_survey = true", name="phase1_corridors_need_survey"),
        CheckConstraint(
            f"source_release ~ '{CONFIG_SHA}'", name="source_release_is_canonical"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False
    )
    corridor_key: Mapped[str] = mapped_column(String(512), nullable=False)
    origin_facility_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("facility.id", ondelete="RESTRICT"), nullable=False
    )
    destination_facility_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("facility.id", ondelete="RESTRICT"), nullable=False
    )
    centerline: Mapped[Any | None] = mapped_column(
        Geometry("LineString", srid=4326, spatial_index=False)
    )
    floor_altitude_m: Mapped[float | None] = mapped_column(Numeric(10, 3))
    floor_altitude_reference: Mapped[str] = mapped_column(
        pg_enum("altitude_reference"), nullable=False, server_default=text("'AGL'")
    )
    ceiling_altitude_m: Mapped[float | None] = mapped_column(Numeric(10, 3))
    ceiling_altitude_reference: Mapped[str] = mapped_column(
        pg_enum("altitude_reference"), nullable=False, server_default=text("'AGL'")
    )
    max_wind_ms: Mapped[float | None] = mapped_column(Numeric(8, 3))
    lane_rules_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    status: Mapped[str] = mapped_column(
        pg_enum("corridor_status"), nullable=False, server_default=text("'open'")
    )
    closure_window: Mapped[Any | None] = mapped_column(TSTZRANGE)
    source_release: Mapped[str] = mapped_column(String(71), nullable=False)
    synthetic: Mapped[bool] = mapped_column(
        nullable=False, server_default=text("true"), default=True
    )
    requires_survey: Mapped[bool] = mapped_column(
        nullable=False, server_default=text("true"), default=True
    )


class RunMetric(Base, TenantMixin):
    """A metric with its formula and denominator, so a number can be argued with."""

    __tablename__ = "run_metric"
    __table_args__: tuple[Any, ...] = (
        tenant_index("run_metric"),
        UniqueConstraint("run_id", "metric_name", "window_key", name="one_value_per_metric_window"),
        CheckConstraint("length(btrim(metric_name)) > 0", name="metric_name_is_not_blank"),
        CheckConstraint("length(btrim(formula)) > 0", name="formula_is_recorded"),
        CheckConstraint("length(btrim(unit)) > 0", name="unit_is_recorded"),
        # Rejected work must be visible: a metric row may be a rejection count, and the
        # denominator being zero is recorded rather than hidden.
        CheckConstraint("denominator >= 0", name="denominator_is_not_negative"),
        CheckConstraint(
            "denominator = 0 or numerator is not null", name="numerator_is_recorded"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("sim_run.id", ondelete="CASCADE"), nullable=False
    )
    metric_name: Mapped[str] = mapped_column(String(200), nullable=False)
    value_numeric: Mapped[float | None] = mapped_column(Numeric(20, 6))
    numerator: Mapped[int | None] = mapped_column(BigInteger)
    denominator: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    unit: Mapped[str] = mapped_column(String(64), nullable=False)
    formula: Mapped[str] = mapped_column(Text, nullable=False)
    window_key: Mapped[str] = mapped_column(String(200), nullable=False, server_default=text("'whole-run'"))
    state: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'observed'")
    )
