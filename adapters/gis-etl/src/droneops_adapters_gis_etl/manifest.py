"""The data manifest (spec 12 section 9), written as ``manifest.yaml``.

This file is the snapshot's only public statement about itself. It is deliberately
boring and complete: what was retrieved, when, from where, with what hash, under what
licence, what was *not* retrieved, and what must be surveyed before any of it can be
used for a real operation.

It never says the word safe without denying it, and ``authoritative: false`` is the
first content key in the document so nobody has to read to the end to learn it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from .config import GisConfig
from .errors import GisEtlError
from .provenance import OSM_ATTRIBUTION, OSM_LICENSE, utc_now

NOT_AUTHORISED = "Simulation only. Not authorised for flight."

REQUIRED_KEYS = (
    "schema_version",
    "dataset_id",
    "authoritative",
    "retrieved_at",
    "boundary",
    "corridor",
    "terrain",
    "obstacles",
)

#: The forbidden promotion: synthetic layers must never look operational.
FORBIDDEN_PROMOTION_KEYS = (
    "operational",
    "certified",
    "approved",
    "authorised_for_flight",
    "cleared",
)


@dataclass
class RunRecord:
    """Everything the pipeline learned while producing the snapshot."""

    started_at: datetime
    finished_at: datetime
    live_components: int = 0
    cached_components: int = 0
    failed_components: int = 0
    boundary: dict[str, Any] = field(default_factory=dict)
    layers: dict[str, Any] = field(default_factory=dict)
    obstacles: dict[str, Any] = field(default_factory=dict)
    corridors: dict[str, Any] = field(default_factory=dict)
    coverage: dict[str, Any] = field(default_factory=dict)
    extract: dict[str, Any] = field(default_factory=dict)
    attempts: list[dict[str, Any]] = field(default_factory=list)
    degradations: list[str] = field(default_factory=list)
    files: dict[str, Any] = field(default_factory=dict)

    @property
    def all_live(self) -> bool:
        return self.failed_components == 0 and self.cached_components == 0


def build_manifest(config: GisConfig, run: RunRecord) -> dict[str, Any]:
    """Assemble the manifest document. Raises if the evidence for a claim is missing."""
    coverage = run.coverage or {}
    metrics = {str(item["name"]): item for item in coverage.get("metrics", [])}
    boundary = run.boundary or {}
    if not boundary.get("hash"):
        raise GisEtlError(
            "cannot write a manifest without a boundary hash: an unhashable boundary is "
            "an unprovable one"
        )

    height = metrics.get("tagged_height", {}).get("total")
    buildings = metrics.get("building_ways", {}).get("total")
    coverage_string = (
        f"{height:,} of {buildings:,} tagged height"
        if isinstance(height, int) and isinstance(buildings, int)
        else f"{config.baseline.get('tagged_height', 'unknown'):,} of "
        f"{config.baseline.get('building_ways', 'unknown'):,} tagged height (quoted from "
        f"{coverage.get('baseline_retrieved_at', 'the 30 September 2026 measurement')}, "
        f"not re-measured in this run)"
    )

    document: dict[str, Any] = {
        "schema_version": 1,
        "dataset_id": config.dataset_id,
        "authoritative": False,
        "authoritative_note": (
            "No value in this dataset is an approved, surveyed or certified aviation "
            "datum. The horizontal network is OpenStreetMap; the vertical data is "
            "synthetic. " + NOT_AUTHORISED
        ),
        "retrieved_at": run.started_at.isoformat(),
        "produced_at": run.finished_at.isoformat(),
        "configuration": {
            "file": "configs/gis/chandigarh-synthetic-v1.yaml",
            "config_version": config.config_version,
            "sha256": None,  # filled by write_manifest, which knows the file
        },
        "region": {
            "name": config.region_name,
            "service_bbox_deg": list(config.service_bbox.as_tuple()),
            "service_bbox_km": [
                round(config.service_bbox.height_km(), 2),
                round(config.service_bbox.width_km(config.reference_latitude_deg), 2),
            ],
            "central_bbox_deg": list(config.central_bbox.as_tuple()),
        },
        "boundary": {
            "source": "OpenStreetMap",
            "relation_id": config.boundary_relation_id,
            "name": boundary.get("name", ""),
            "admin_level": boundary.get("admin_level", ""),
            "iso3166_2": boundary.get("iso3166_2", ""),
            "wikidata": boundary.get("wikidata", ""),
            "hash": boundary["hash"],
            "licence": boundary.get("licence", OSM_LICENSE),
            "attribution": boundary.get("attribution", OSM_ATTRIBUTION),
            "retrieved_at": boundary.get("retrieved_at", ""),
            "endpoint": boundary.get("endpoint", ""),
            "query_sha256": boundary.get("query_sha256", ""),
            "role": "display mask only",
            "is_flight_legality_evidence": False,
            "note": (
                "The administrative boundary is used to clip and to draw. It is not "
                "evidence that any point inside it may be flown, and the airport "
                "civil-enclave boundary, red/yellow zone geometry and military "
                "coordination limits are NOT represented here (spec 12 section 8)."
            ),
        },
        "corridor": {
            "method": config.corridor.get("method", "synthetic"),
            "basis": config.corridor.get("basis", "road and green-space heuristic"),
            "safety_claim": False,
            "planning_heuristic_only": True,
            "corridors": run.corridors.get("corridors", 0),
            "total_length_m": run.corridors.get("total_length_m", 0),
            "requires_survey": True,
            "hash": run.corridors.get("hash", ""),
            "note": (
                "A drone is not safer for flying above a road. Wires cross it, traffic "
                "moves under it, trees lean into it and the rooftops beside it are "
                "structures this dataset has no height for."
            ),
            "not_a_safety_argument": True,
        },
        "terrain": _terrain_section(config, run),
        "obstacles": {
            "building_height_coverage": coverage_string,
            "building_height_coverage_source": (
                "measured by this run" if isinstance(height, int) else "quoted baseline"
            ),
            "wire_source": "OpenStreetMap lines, assumed vertical clearance",
            "requires_survey": True,
            "count": run.obstacles.get("obstacles", 0),
            "by_kind": run.obstacles.get("by_kind", {}),
            "by_height_basis": run.obstacles.get("by_height_basis", {}),
            "vertical_model": {
                "version": config.vertical.version,
                "metres_per_level": config.vertical.metres_per_level,
                "untagged_residential_height_m": config.vertical.untagged_residential_height_m,
                "untagged_commercial_height_m": config.vertical.untagged_commercial_height_m,
                "untagged_default_height_m": config.vertical.untagged_default_height_m,
                "power_line_assumed_height_m": config.vertical.power_line_assumed_height_m,
                "unknown_area_extra_margin_m": config.vertical.unknown_area_extra_margin_m,
                "height_cap_m": config.vertical.height_cap_m,
                "altitude_reference": config.vertical.altitude_reference,
            },
            "flags_on_every_obstacle": {
                "obstacle_source": "synthetic",
                "vertical_authority": "none",
                "requires_survey": True,
                "synthetic": True,
                "authoritative": False,
            },
            "hash": run.obstacles.get("hash", ""),
        },
        "coverage_evidence": _coverage_section(config, coverage, metrics),
        "layers": _layers_section(run),
        "provenance": {
            "components_live": run.live_components,
            "components_from_cache": run.cached_components,
            "components_failed": run.failed_components,
            "degradations": run.degradations,
            "http_attempts": run.attempts,
            "license": OSM_LICENSE,
            "attribution": OSM_ATTRIBUTION,
            "tile_usage_note": (
                "No raster tiles were used. The public OSM tile service is not a bulk "
                "production source; this dataset is Overpass data plus local extracts."
            ),
        },
        "files": run.files,
        "extract": run.extract or {"used": False, "note": "no bulk extract in this run; Overpass only"},
        "what_this_proves": [
            "a map-based workflow can ingest and display Chandigarh features",
            "OSM provides the horizontal network here",
            "a deterministic synthetic obstacle and corridor model can be generated",
        ],
        "what_this_does_not_prove": [
            "that any proposed corridor has obstacle clearance",
            "that any route here is legally permitted",
            "that any hub, pad or node site exists or is reachable",
            "that weather, wind or local restrictions are represented",
            "that the airport civil-enclave boundary or red/yellow zones are correct",
            "that any aircraft could fly the demonstrated path",
        ],
        "upgrade_path": [
            "synthetic boundary and graph",
            "official airspace snapshot from an authorised source",
            "site survey and pad validation",
            "surveyed or approved obstacle data",
            "validated terrain and micro-weather",
            "approved corridor policy",
            "safety-reviewed route library",
        ],
        "promotion_rule": (
            "Synthetic layers are never silently promoted to operational layers. Each "
            "promotion above is a data change with its own source, hash and review."
        ),
        "statement": NOT_AUTHORISED,
    }
    _validate(document)
    return document


def _terrain_section(config: GisConfig, run: RunRecord) -> dict[str, Any]:
    terrain = dict(config.terrain)
    recorded = run.extract.get("terrain") if isinstance(run.extract, dict) else None
    section = {
        "source": terrain.get("source", "none-supplied"),
        "resolution_m": terrain.get("resolution_m", 30),
        "vertical_datum": terrain.get("vertical_datum", "unknown"),
        "raster_version": terrain.get("raster_version", "unknown"),
        "clipping_method": terrain.get("clipping_method", "polygon-mask-bbox-crop"),
        "retrieval_date": (recorded or {}).get("retrieved_at", ""),
        "hash": (recorded or {}).get("sha256", ""),
        "usable_for_obstacle_clearance": False,
        "caveat": (
            "A 30 m grid cannot represent a tower, a wire or a rooftop. Terrain here is "
            "ground surface only; obstacle clearance is not a terrain question."
        ),
        "no_credentials_note": (
            "No DEM was downloaded. Copernicus GLO-30 and NASADEM require an account and "
            "carry licence terms; this adapter reads a local raster path supplied by an "
            "operator who already has those rights (adapters/terrain)."
        ),
    }
    if section["vertical_datum"] in {"unknown", "", None}:
        section["datum_declared"] = False
        section["datum_note"] = (
            "Vertical datum is not declared, so ground heights cannot be converted to "
            "AGL and are recorded as unusable rather than guessed."
        )
    else:
        section["datum_declared"] = True
    return section


def _coverage_section(
    config: GisConfig, coverage: dict[str, Any], metrics: dict[str, Any]
) -> dict[str, Any]:
    measured = {
        name: item.get("total")
        for name, item in metrics.items()
        if isinstance(item, dict) and "total" in item
    }
    return {
        "measured_at": coverage.get("measured_at", ""),
        "claim_current": bool(coverage.get("claim_current", False)),
        "live_metrics": coverage.get("live_metrics", 0),
        "cached_metrics": coverage.get("cached_metrics", 0),
        "failures": coverage.get("failures", []),
        "measured_counts": measured,
        "baseline_2026_09_30": dict(config.baseline),
        "verdict": coverage.get("verdict", ""),
        "notes": coverage.get("notes", []),
        "queries": [
            {
                "name": name,
                "query_sha256": item.get("query_sha256"),
                "response_sha256": item.get("response_sha256"),
                "raw_path": item.get("raw_path"),
                "endpoint": item.get("endpoint"),
                "retrieved_at": item.get("retrieved_at"),
                "replication_timestamp": item.get("replication_timestamp"),
            }
            for name, item in sorted(metrics.items())
            if isinstance(item, dict)
        ],
    }


def _layers_section(run: RunRecord) -> dict[str, Any]:
    section: dict[str, Any] = {}
    for name, outcome in sorted(run.layers.items()):
        section[name] = {
            "features": outcome.get("features", 0),
            "chunks": outcome.get("chunks", {}),
            "degraded": outcome.get("degraded", False),
            "truncated": outcome.get("truncated", False),
            "dropped_outside_boundary": outcome.get("dropped_outside_boundary", 0),
            "dropped_no_geometry": outcome.get("dropped_no_geometry", 0),
            "files": outcome.get("files", {}),
            "failures": outcome.get("failures", []),
        }
    return section


def _validate(document: dict[str, Any]) -> None:
    missing = [key for key in REQUIRED_KEYS if key not in document]
    if missing:
        raise GisEtlError(f"manifest is missing required keys: {missing}")
    if document["authoritative"] is not False:
        raise GisEtlError("manifest declares itself authoritative; it is not")
    if document["corridor"]["safety_claim"] is not False:
        raise GisEtlError("manifest asserts a corridor safety claim")
    if document["obstacles"]["requires_survey"] is not True:
        raise GisEtlError("manifest claims obstacles need no survey")
    if document["boundary"]["relation_id"] != 1942809:
        raise GisEtlError(
            f"boundary relation {document['boundary']['relation_id']} is not the "
            "Chandigarh administrative relation 1942809"
        )
    if document["boundary"]["licence"] != OSM_LICENSE:
        raise GisEtlError("OSM-derived boundary must be licensed ODbL")
    for section in ("boundary", "corridor", "obstacles", "terrain"):
        for key in document[section]:
            if key in FORBIDDEN_PROMOTION_KEYS:
                raise GisEtlError(f"manifest section {section} carries {key!r}")
    if document["statement"] != NOT_AUTHORISED:
        raise GisEtlError("manifest must state the simulation-only line verbatim")


def write_manifest(document: dict[str, Any], path: Path, *, config_path: Path) -> str:
    """Write YAML (key order preserved) and return the file's sha256."""
    from .hashing import file_sha256

    document["configuration"]["sha256"] = file_sha256(config_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(document, sort_keys=False, allow_unicode=True, width=100)
    body = (
        "# DroneOps Chandigarh synthetic geodata snapshot manifest.\n"
        "# Generated by adapters/gis-etl. Do not edit by hand: re-run the pipeline.\n"
        "# Simulation only. Not authorised for flight.\n" + text
    )
    path.write_text(body, encoding="utf-8")
    return file_sha256(path)


def load_manifest(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))
