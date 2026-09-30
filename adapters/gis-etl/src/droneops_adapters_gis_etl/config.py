"""Typed view over ``configs/gis/chandigarh-synthetic-v1.yaml``.

No synthetic height, no clearance, no separation number lives in code. They are read
from a versioned config file (conventions section 3), validated here, and echoed into
the manifest so the produced snapshot records *which* assumptions built it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .errors import ConfigError

CONFIG_RELPATH = Path("configs") / "gis" / "chandigarh-synthetic-v1.yaml"


def repo_root(start: Path | None = None) -> Path:
    """The monorepo root, found by walking up from this file.

    The adapter is also importable from a test working directory, so no caller gets to
    assume ``Path.cwd()`` is the repo.
    """
    current = (start or Path(__file__)).resolve()
    for candidate in [current, *current.parents]:
        if (candidate / CONFIG_RELPATH).is_file() and (candidate / "pyproject.toml").is_file():
            return candidate
    raise ConfigError(f"could not locate a repo root containing {CONFIG_RELPATH}")


def default_config_path(root: Path | None = None) -> Path:
    return (root or repo_root()) / CONFIG_RELPATH


@dataclass(frozen=True)
class Bbox:
    """A WGS 84 bounding box, south-west/north-east, in decimal degrees."""

    south: float
    west: float
    north: float
    east: float

    def __post_init__(self) -> None:
        if not -90.0 <= self.south < self.north <= 90.0:
            raise ConfigError(f"invalid bbox latitudes {self.south}..{self.north}")
        if not -180.0 <= self.west < self.east <= 180.0:
            raise ConfigError(f"invalid bbox longitudes {self.west}..{self.east}")

    def as_tuple(self) -> tuple[float, float, float, float]:
        return (self.south, self.west, self.north, self.east)

    def as_overpass(self) -> str:
        """Overpass QL bbox fragment, fixed to 6 decimals for a stable query hash."""
        return f"{self.south:.6f},{self.west:.6f},{self.north:.6f},{self.east:.6f}"

    def width_km(self, reference_lat_deg: float) -> float:
        import math

        per_deg = 111.320 * math.cos(math.radians(reference_lat_deg))
        return (self.east - self.west) * per_deg

    def height_km(self) -> float:
        return (self.north - self.south) * 110.574

    def split(self, rows: int, cols: int) -> list[Bbox]:
        """A chunk grid covering this box.

        Chunking is what makes a big layer survivable on a public endpoint: one 504
        costs one chunk, and a cached chunk is never re-requested.
        """
        if rows < 1 or cols < 1:
            raise ConfigError("chunk grid must be at least 1x1")
        lat_step = (self.north - self.south) / rows
        lon_step = (self.east - self.west) / cols
        chunks: list[Bbox] = []
        for r in range(rows):
            for c in range(cols):
                chunks.append(
                    Bbox(
                        south=self.south + r * lat_step,
                        west=self.west + c * lon_step,
                        north=self.south + (r + 1) * lat_step,
                        east=self.west + (c + 1) * lon_step,
                    )
                )
        return chunks


@dataclass(frozen=True)
class OverpassSettings:
    user_agent: str
    endpoints: tuple[str, ...]
    request_timeout_s: float
    max_attempts_per_endpoint: int
    backoff_base_s: float
    backoff_multiplier: float
    backoff_cap_s: float
    jitter_max_s: float
    retry_seed: int
    request_minimum_interval_s: float
    endpoint_cooldown_s: float
    allow_live: bool
    require_iso_replication_timestamp: bool
    reject_zero_when_baseline_nonzero: bool
    max_consecutive_suspicious: int

    def validate(self) -> None:
        if not self.user_agent or "DroneOps" not in self.user_agent:
            raise ConfigError(
                "overpass.user_agent must identify the project; the public endpoints "
                "answer HTTP 406 to an anonymous or generic agent"
            )
        if not self.endpoints:
            raise ConfigError("overpass.endpoints must not be empty")
        if self.request_minimum_interval_s < 1.0:
            raise ConfigError(
                "overpass.request_minimum_interval_s below 1 s would hammer a volunteer "
                "endpoint; that is explicitly out of the rules (spec 12 section 3)"
            )
        if self.max_attempts_per_endpoint < 1:
            raise ConfigError("overpass.max_attempts_per_endpoint must be >= 1")


@dataclass(frozen=True)
class VerticalModel:
    version: str
    metres_per_level: float
    untagged_residential_height_m: float
    untagged_commercial_height_m: float
    untagged_default_height_m: float
    power_line_assumed_height_m: float
    tower_assumed_height_m: float
    mast_assumed_height_m: float
    tree_assumed_height_m: float
    unknown_area_extra_margin_m: float
    uncertainty_ratio: float
    height_cap_m: float
    altitude_reference: str
    residential_buildings: frozenset[str]
    commercial_buildings: frozenset[str]

    def validate(self) -> None:
        for name in (
            "metres_per_level",
            "untagged_residential_height_m",
            "untagged_commercial_height_m",
            "untagged_default_height_m",
            "power_line_assumed_height_m",
            "tower_assumed_height_m",
            "mast_assumed_height_m",
            "tree_assumed_height_m",
        ):
            if getattr(self, name) <= 0:
                raise ConfigError(f"synthetic_vertical_model.{name} must be positive")
        if self.height_cap_m <= 0:
            raise ConfigError("synthetic_vertical_model.height_cap_m must be positive")
        if self.uncertainty_ratio < 0:
            raise ConfigError("synthetic_vertical_model.uncertainty_ratio must be >= 0")
        if self.unknown_area_extra_margin_m < 0:
            raise ConfigError("synthetic_vertical_model.unknown_area_extra_margin_m >= 0")
        if self.altitude_reference not in {"AGL", "AMSL", "GROUND", "PAD"}:
            raise ConfigError(
                "synthetic_vertical_model.altitude_reference must be an explicit "
                "reference (I9); UNSPECIFIED is not permitted on a generated height"
            )


@dataclass(frozen=True)
class LayerSpec:
    name: str
    enabled: bool
    output: str
    chunk_grid: int
    max_features: int

    def validate(self) -> None:
        if self.output not in {"center", "geom"}:
            raise ConfigError(f"layers.{self.name}.output must be 'center' or 'geom'")
        if self.chunk_grid < 1:
            raise ConfigError(f"layers.{self.name}.chunk_grid must be >= 1")
        if self.max_features < 1:
            raise ConfigError(f"layers.{self.name}.max_features must be >= 1")


@dataclass(frozen=True)
class GisConfig:
    root: Path
    dataset_id: str
    config_version: str
    authoritative: bool
    region_name: str
    boundary_relation_id: int
    expected_boundary_name: str
    boundary_licence: str
    boundary_attribution: str
    service_bbox: Bbox
    central_bbox: Bbox
    reference_latitude_deg: float
    overpass: OverpassSettings
    raw_dir: Path
    processed_dir: Path
    layers: dict[str, LayerSpec]
    vertical: VerticalModel
    corridor: dict[str, Any]
    terrain: dict[str, Any]
    baseline: dict[str, Any]
    snapshot_ttl_days: int
    licence: str
    attribution: str
    metric_primary_timeout_s: int = 30
    metric_chunk_timeout_s: int = 25
    metric_chunk_grid: int = 2
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def repo_relative_raw_dir(self) -> str:
        return self.raw_dir.relative_to(self.root).as_posix()

    @property
    def repo_relative_processed_dir(self) -> str:
        return self.processed_dir.relative_to(self.root).as_posix()

    def validate(self) -> None:
        if self.authoritative:
            raise ConfigError(
                "this dataset is synthetic and must not declare itself authoritative"
            )
        self.overpass.validate()
        self.vertical.validate()
        for spec in self.layers.values():
            spec.validate()
        if self.corridor.get("safety_claim") is not False:
            raise ConfigError(
                "corridor.safety_claim must be false: no candidate corridor derived "
                "from road alignment may claim safety (spec 12 section 7)"
            )


def _require(mapping: dict[str, Any], key: str, where: str) -> Any:
    if key not in mapping:
        raise ConfigError(f"{where}: missing required key {key!r}")
    return mapping[key]


def _bbox(raw: dict[str, Any], where: str) -> Bbox:
    return Bbox(
        south=float(_require(raw, "south", where)),
        west=float(_require(raw, "west", where)),
        north=float(_require(raw, "north", where)),
        east=float(_require(raw, "east", where)),
    )


def load_config(path: Path | str | None = None, *, root: Path | None = None) -> GisConfig:
    """Read and validate the versioned ETL config."""
    base = root or repo_root()
    config_path = Path(path) if path else default_config_path(base)
    if not config_path.is_file():
        raise ConfigError(f"config not found: {config_path}")
    raw: dict[str, Any] = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ConfigError(f"config {config_path} did not parse to a mapping")

    region = _require(raw, "region", "config")
    boundary = _require(region, "administrative_boundary", "region")
    op_raw = _require(raw, "overpass", "config")
    guard = op_raw.get("plausibility_guard", {})
    paths = _require(raw, "paths", "config")
    vertical_raw = _require(raw, "synthetic_vertical_model", "config")
    classification = vertical_raw.get("classification", {})
    layers_raw = _require(raw, "layers", "config")

    settings = OverpassSettings(
        user_agent=str(_require(op_raw, "user_agent", "overpass")),
        endpoints=tuple(op_raw.get("endpoints", ())),
        request_timeout_s=float(op_raw.get("request_timeout_s", 60)),
        max_attempts_per_endpoint=int(op_raw.get("max_attempts_per_endpoint", 4)),
        backoff_base_s=float(op_raw.get("backoff_base_s", 3.0)),
        backoff_multiplier=float(op_raw.get("backoff_multiplier", 2.0)),
        backoff_cap_s=float(op_raw.get("backoff_cap_s", 45.0)),
        jitter_max_s=float(op_raw.get("jitter_max_s", 2.0)),
        retry_seed=int(op_raw.get("retry_seed", 0)),
        request_minimum_interval_s=float(op_raw.get("request_minimum_interval_s", 4.0)),
        endpoint_cooldown_s=float(op_raw.get("endpoint_cooldown_s", 90.0)),
        allow_live=bool(op_raw.get("allow_live", True)),
        require_iso_replication_timestamp=bool(
            guard.get("require_iso_replication_timestamp", True)
        ),
        reject_zero_when_baseline_nonzero=bool(
            guard.get("reject_zero_when_baseline_nonzero", True)
        ),
        max_consecutive_suspicious=int(guard.get("max_consecutive_suspicious", 2)),
    )

    layers = {
        name: LayerSpec(
            name=name,
            enabled=bool(values.get("enabled", True)),
            output=str(values.get("output", "center")),
            chunk_grid=int(values.get("chunk_grid", 1)),
            max_features=int(values.get("max_features", 10000)),
        )
        for name, values in layers_raw.items()
        if isinstance(values, dict)
    }

    config = GisConfig(
        root=base,
        dataset_id=str(_require(raw, "dataset_id", "config")),
        config_version=str(raw.get("config_version", "unversioned")),
        authoritative=bool(_require(raw, "authoritative", "config")),
        region_name=str(region.get("name", "")),
        boundary_relation_id=int(_require(boundary, "osm_relation_id", "boundary")),
        expected_boundary_name=str(boundary.get("expected_name", "")),
        boundary_licence=str(boundary.get("licence", "ODbL")),
        boundary_attribution=str(boundary.get("attribution", "")),
        service_bbox=_bbox(_require(region, "service_bbox", "region"), "service_bbox"),
        central_bbox=_bbox(_require(region, "central_bbox", "region"), "central_bbox"),
        reference_latitude_deg=float(region.get("reference_latitude_deg", 30.73)),
        overpass=settings,
        raw_dir=(base / str(_require(paths, "raw_dir", "paths"))).resolve(),
        processed_dir=(base / str(_require(paths, "processed_dir", "paths"))).resolve(),
        layers=layers,
        vertical=VerticalModel(
            version=str(_require(vertical_raw, "version", "synthetic_vertical_model")),
            metres_per_level=float(_require(vertical_raw, "metres_per_level", "model")),
            untagged_residential_height_m=float(vertical_raw["untagged_residential_height_m"]),
            untagged_commercial_height_m=float(vertical_raw["untagged_commercial_height_m"]),
            untagged_default_height_m=float(vertical_raw["untagged_default_height_m"]),
            power_line_assumed_height_m=float(vertical_raw["power_line_assumed_height_m"]),
            tower_assumed_height_m=float(
                vertical_raw.get("tower_assumed_height_m", vertical_raw["untagged_default_height_m"])
            ),
            mast_assumed_height_m=float(
                vertical_raw.get("mast_assumed_height_m", vertical_raw["untagged_default_height_m"])
            ),
            tree_assumed_height_m=float(
                vertical_raw.get("tree_assumed_height_m", vertical_raw["untagged_default_height_m"])
            ),
            unknown_area_extra_margin_m=float(vertical_raw["unknown_area_extra_margin_m"]),
            uncertainty_ratio=float(vertical_raw["uncertainty_ratio"]),
            height_cap_m=float(vertical_raw["height_cap_m"]),
            altitude_reference=str(vertical_raw.get("altitude_reference", "AGL")),
            residential_buildings=frozenset(classification.get("residential", ())),
            commercial_buildings=frozenset(classification.get("commercial", ())),
        ),
        corridor=dict(_require(raw, "corridor", "config")),
        terrain=dict(raw.get("terrain", {})),
        baseline=dict(_require(raw, "coverage_metrics", "config").get("baseline", {})),
        metric_primary_timeout_s=int(raw.get("coverage_metrics", {}).get("primary_timeout_s", 30)),
        metric_chunk_timeout_s=int(raw.get("coverage_metrics", {}).get("chunk_timeout_s", 25)),
        metric_chunk_grid=max(1, int(raw.get("coverage_metrics", {}).get("chunk_grid", 2))),
        snapshot_ttl_days=int(raw.get("provenance_defaults", {}).get("snapshot_ttl_days", 30)),
        licence=str(raw.get("provenance_defaults", {}).get("licence", "ODbL")),
        attribution=str(raw.get("provenance_defaults", {}).get("attribution", "")),
        raw=raw,
    )
    config.validate()
    return config
