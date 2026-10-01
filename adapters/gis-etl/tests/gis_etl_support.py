"""Shared builders for the gis-etl tests.

These live here rather than in ``conftest.py`` on purpose: prepend import mode makes
``conftest`` a single flat module name across the repository, so ``from conftest
import X`` silently resolves to whichever package's conftest was imported first. The
``adapters/*/tests`` directory may not reach the network; every client test runs
against ``httpx.MockTransport``, and the live retrieval this workstream produced is
re-read from ``data/raw`` instead of re-fetched.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from droneops_adapters_gis_etl.cache import RawCache
from droneops_adapters_gis_etl.config import GisConfig, load_config
from droneops_adapters_gis_etl.overpass import OverpassClient

CONFIG_RELPATH = Path("configs") / "gis" / "chandigarh-synthetic-v1.yaml"

RETRIEVED_AT = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)
GOOD_TIMESTAMP = "2026-09-30T19:49:08Z"


def config_template() -> dict[str, Any]:
    """A full, valid config with test-scale limits (1 s spacing, no sleeping)."""
    return {
        "schema_version": 1,
        "config_version": "test-1.0.0",
        "dataset_id": "test-chandigarh-v1",
        "authoritative": False,
        "region": {
            "name": "Chandigarh (Union Territory of Chandigarh, IN-CH)",
            "administrative_boundary": {
                "osm_relation_id": 1942809,
                "expected_name": "Chandigarh",
                "licence": "ODbL",
                "attribution": "(c) OpenStreetMap contributors",
            },
            "service_bbox": {"south": 30.68, "west": 76.72, "north": 30.78, "east": 76.86},
            "central_bbox": {"south": 30.73, "west": 76.78, "north": 30.75, "east": 76.8},
            "reference_latitude_deg": 30.73,
        },
        "overpass": {
            "user_agent": "DroneOps-GisETL-test/1.0 (contact: geo@droneops.invalid)",
            "endpoints": ["https://mock.invalid/api/interpreter", "https://backup.invalid/api/interpreter"],
            "request_timeout_s": 5,
            "max_attempts_per_endpoint": 3,
            "backoff_base_s": 1.0,
            "backoff_multiplier": 2.0,
            "backoff_cap_s": 10.0,
            "jitter_max_s": 0.5,
            "retry_seed": 4242,
            "request_minimum_interval_s": 1.0,
            "endpoint_cooldown_s": 30.0,
            "allow_live": True,
            "plausibility_guard": {
                "require_iso_replication_timestamp": True,
                "reject_zero_when_baseline_nonzero": True,
                "max_consecutive_suspicious": 2,
            },
        },
        "paths": {"raw_dir": "data/raw", "processed_dir": "data/processed/test-chandigarh-v1"},
        "coverage_metrics": {
            "baseline": {
                "retrieved_at": "2026-09-30T00:00:00Z",
                "building_ways": 1000,
                "tagged_height": 1,
                "tagged_building_levels": 20,
                "power_towers": 50,
                "power_lines": 100,
                "highway_ways": 2000,
                "aeroway_ways": 5,
                "man_made_towers": 2,
                "trees": 3,
                "central_buildings": 300,
                "central_with_height": 0,
            }
        },
        "layers": {
            "boundary": {"enabled": True, "output": "geom", "chunk_grid": 1, "max_features": 10},
            "buildings": {"enabled": True, "output": "center", "chunk_grid": 1, "max_features": 500},
            "power_lines": {"enabled": True, "output": "geom", "chunk_grid": 1, "max_features": 500},
            "towers": {"enabled": True, "output": "center", "chunk_grid": 1, "max_features": 500},
            "trees": {"enabled": True, "output": "center", "chunk_grid": 1, "max_features": 500},
            "roads": {"enabled": True, "output": "geom", "chunk_grid": 1, "max_features": 500},
            "parks": {"enabled": True, "output": "center", "chunk_grid": 1, "max_features": 500},
        },
        "synthetic_vertical_model": {
            "version": "test-vertical-1.0",
            "metres_per_level": 3.2,
            "untagged_residential_height_m": 6.4,
            "untagged_commercial_height_m": 12.8,
            "untagged_default_height_m": 9.6,
            "power_line_assumed_height_m": 8.0,
            "tower_assumed_height_m": 30.0,
            "mast_assumed_height_m": 25.0,
            "tree_assumed_height_m": 12.0,
            "unknown_area_extra_margin_m": 10.0,
            "uncertainty_ratio": 0.5,
            "height_cap_m": 60.0,
            "altitude_reference": "AGL",
            "classification": {
                "residential": ["residential", "house"],
                "commercial": ["commercial", "school", "hospital"],
            },
        },
        "corridor": {
            "method": "synthetic",
            "basis": "road and green-space heuristic",
            "safety_claim": False,
            "planning_heuristic_only": True,
            "road_classes": ["primary", "secondary"],
            "grouping_tolerance_m": 120.0,
            "min_corridor_length_m": 100.0,
            "max_corridors": 20,
        },
        "terrain": {
            "source": "none-supplied",
            "resolution_m": 30,
            "vertical_datum": "unknown",
            "raster_version": "unknown",
            "clipping_method": "polygon-mask-bbox-crop",
            "usable_for_obstacle_clearance": False,
        },
        "provenance_defaults": {
            "licence": "ODbL",
            "attribution": "(c) OpenStreetMap contributors",
            "snapshot_ttl_days": 30,
        },
    }


def write_config_root(root: Path) -> Path:
    """A throwaway repo root: a config, a pyproject marker and empty data directories."""
    (root / "pyproject.toml").write_text("[project]\nname = 'test-root'\n", encoding="utf-8")
    target = root / CONFIG_RELPATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump(config_template(), sort_keys=False), encoding="utf-8")
    (root / "data" / "raw").mkdir(parents=True, exist_ok=True)
    return root


def load_test_config(root: Path) -> GisConfig:
    loaded = load_config(root / CONFIG_RELPATH, root=root)
    loaded.processed_dir.mkdir(parents=True, exist_ok=True)
    return loaded


def overpass_payload(elements: list[dict[str, Any]], *, timestamp: str = GOOD_TIMESTAMP) -> dict[str, Any]:
    return {
        "version": 0.6,
        "generator": "Overpass API test",
        "osm3s": {"timestamp_osm_base": timestamp, "copyright": "ODbL"},
        "elements": elements,
    }


def count_payload(ways: int, *, timestamp: str = GOOD_TIMESTAMP) -> dict[str, Any]:
    return overpass_payload(
        [
            {
                "type": "count",
                "id": 0,
                "tags": {
                    "nodes": "0",
                    "ways": str(ways),
                    "relations": "0",
                    "total": str(ways),
                },
            }
        ],
        timestamp=timestamp,
    )


def building_node(
    osm_id: int,
    *,
    tags: dict[str, str],
    lat: float = 30.74,
    lon: float = 76.79,
) -> dict[str, Any]:
    return {"type": "node", "id": osm_id, "lat": lat, "lon": lon, "tags": {"building": "residential", **tags}}


def wire_way(osm_id: int, *, tags: dict[str, str] | None = None) -> dict[str, Any]:
    return {
        "type": "way",
        "id": osm_id,
        "tags": {"power": "line", **(tags or {})},
        "geometry": [
            {"lat": 30.74, "lon": 76.78},
            {"lat": 30.741, "lon": 76.79},
            {"lat": 30.742, "lon": 76.80},
        ],
    }


def road_way(osm_id: int, *, highway: str = "primary", offset: float = 0.0) -> dict[str, Any]:
    return {
        "type": "way",
        "id": osm_id,
        "tags": {"highway": highway},
        "geometry": [
            {"lat": 30.740 + offset, "lon": 76.780},
            {"lat": 30.745 + offset, "lon": 76.790},
        ],
    }


class MockTransport:
    """A scriptable endpoint: a list of (status, body) answers, consumed in order."""

    def __init__(self, answers: list[Any]) -> None:
        self.answers = list(answers)
        self.requests: list[dict[str, Any]] = []

    def handler(self, request: Any) -> Any:
        import httpx

        self.requests.append(
            {
                "url": str(request.url),
                "user_agent": request.headers.get("user-agent"),
                "body": request.content.decode("utf-8", "replace"),
            }
        )
        answer = self.answers.pop(0) if self.answers else (504, "server busy")
        if isinstance(answer, Exception):
            raise answer
        status, body = answer
        content = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        return httpx.Response(status, content=content, headers={"Content-Type": "application/json"})


class SleepRecorder:
    """Stands in for ``time.sleep`` and records what the client *would* have waited."""

    def __init__(self) -> None:
        self.slept: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.slept.append(seconds)


def make_client(
    config: GisConfig,
    cache: RawCache,
    answers: list[Any],
    *,
    live: bool = True,
    sleeper: Any = None,
) -> tuple[OverpassClient, MockTransport]:
    import httpx

    transport = MockTransport(answers)
    # A monotonic clock that jumps a minute per read: the rate limiter is never the
    # thing under test here, and it must not add real sleeps to the recorded backoff.
    ticks = iter([index * 60.0 for index in range(10_000)])
    client = OverpassClient(
        config.overpass,
        cache,
        live=live,
        transport=httpx.MockTransport(transport.handler),
        sleeper=sleeper or SleepRecorder(),
        clock=lambda: next(ticks),
    )
    return client, transport
