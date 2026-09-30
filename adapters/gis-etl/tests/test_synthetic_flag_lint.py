"""The lint test for synthetic-data honesty (conventions.md section 3).

This is the test the whole workstream exists to make meaningful. It does not call the
generator and trust its output: it re-reads the artefacts from disk, the same bytes a
console or a scenario bundle would read, and requires all five flags on *every*
generated obstacle. It also proves itself - a stripped flag must make it fail, so a
future refactor cannot quietly pass by asserting nothing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from conftest import RETRIEVED_AT, building_node, wire_way

from droneops_adapters_gis_etl.audit import _audit_obstacles, audit_snapshot
from droneops_adapters_gis_etl.config import GisConfig, repo_root
from droneops_adapters_gis_etl.errors import GisEtlError
from droneops_adapters_gis_etl.extract import LayerExtractor
from droneops_adapters_gis_etl.provenance import RetrievalProvenance, assert_synthetic_flags
from droneops_adapters_gis_etl.vertical import build_obstacles, write_obstacles

REQUIRED_FLAGS = ("obstacle_source", "vertical_authority", "requires_survey", "synthetic", "authoritative")

SNAPSHOT = repo_root() / "data" / "processed" / "chandigarh-synthetic-v1"


def _feature(element: dict[str, Any], layer: str) -> dict[str, Any]:
    from droneops_adapters_gis_etl.geom import (
        element_geometry,
        element_properties,
        make_feature,
        round_value,
    )

    provenance = RetrievalProvenance(
        source="OpenStreetMap via Overpass API (https://mock.invalid)",
        retrieved_at=RETRIEVED_AT,
        query_sha256="a" * 64,
        response_sha256="sha256:" + "b" * 64,
        raw_path="data/raw/overpass/z.json",
    ).as_dict()
    properties = element_properties(element, layer=layer)
    properties["height_tag_m"] = None
    properties["levels_tag"] = None
    properties["provenance"] = provenance
    return make_feature(round_value(element_geometry(element) or {}), properties)


def _write_snapshot(config: GisConfig, directory: Path) -> Path:
    obstacles = build_obstacles(
        config,
        buildings=[
            _feature(building_node(1, tags={"building": "residential"}), "buildings"),
            _feature(building_node(2, tags={"building": "school", "building:levels": "3"}), "buildings"),
        ],
        power_lines=[_feature(wire_way(3), "power_lines")],
        towers=[],
        trees=[],
    )
    directory.mkdir(parents=True, exist_ok=True)
    write_obstacles(obstacles, directory / "obstacles.jsonl", directory / "obstacles_summary.json")
    return directory / "obstacles.jsonl"


def test_every_generated_obstacle_on_disk_carries_all_five_flags(
    config: GisConfig, tmp_path: Path
) -> None:
    path = _write_snapshot(config, tmp_path)
    result = _audit_obstacles(path)
    assert result["count"] == 3, result
    assert result["problems"] == []
    assert result["fully_flagged"] == result["count"]
    for line in path.read_text(encoding="utf-8").splitlines():
        properties = json.loads(line)["properties"]
        for flag in REQUIRED_FLAGS:
            assert flag in properties, f"{properties['obstacle_id']} lacks {flag}"
        assert properties["obstacle_source"] == "synthetic"
        assert properties["vertical_authority"] == "none"
        assert properties["requires_survey"] is True
        assert properties["synthetic"] is True
        assert properties["authoritative"] is False


def test_stripping_a_flag_makes_the_lint_fail(config: GisConfig, tmp_path: Path) -> None:
    """The lint is only worth having if it can actually fail."""
    path = _write_snapshot(config, tmp_path)
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    del lines[1]["properties"]["vertical_authority"]
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")

    result = _audit_obstacles(path)
    assert result["problems"], "the audit passed although a flag was removed"
    assert "vertical_authority" in result["problems"][0]

    with pytest.raises(GisEtlError, match="vertical_authority"):
        assert_synthetic_flags(lines[1]["properties"], lines[1]["properties"]["obstacle_id"])


def test_a_flag_with_the_wrong_value_also_fails(config: GisConfig, tmp_path: Path) -> None:
    """``requires_survey: false`` is worse than absent: it actively denies the need."""
    path = _write_snapshot(config, tmp_path)
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    lines[0]["properties"]["requires_survey"] = False
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    result = _audit_obstacles(path)
    assert result["problems"], "a denied survey requirement slipped through"


def test_shipped_snapshot_is_flag_complete() -> None:
    """The produced Chandigarh snapshot, audited from disk. Skips only before a run."""
    obstacles = SNAPSHOT / "obstacles" / "obstacles.jsonl"
    if not obstacles.is_file():
        pytest.skip("no produced snapshot yet; run the gis-etl pipeline first")
    result = _audit_obstacles(obstacles)
    assert result["count"] > 0
    assert result["problems"] == [], result["problems"][:3]
    assert result["fully_flagged"] == result["count"]


def test_full_snapshot_audit_passes_on_the_produced_dataset() -> None:
    from droneops_adapters_gis_etl.config import load_config

    if not (SNAPSHOT / "manifest.yaml").is_file():
        pytest.skip("no produced snapshot manifest yet; run the gis-etl pipeline first")
    config = load_config()
    report = audit_snapshot(config)
    assert report["ok"], report
    assert report["obstacles"]["count"] > 0
    assert report["manifest"]["present"] is True
