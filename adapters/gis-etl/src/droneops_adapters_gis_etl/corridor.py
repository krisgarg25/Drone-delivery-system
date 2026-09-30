"""Candidate corridors: a visual and planning heuristic, nothing more (spec 12 section 7).

Roads and green space are aligned, legible and continuous, which makes them good
*drawing* material for a candidate network. That is the entire justification. A drone
is not safer for flying above a road: the wires cross it, the traffic moves under it,
the trees lean into it and the rooftops that line it are exactly the structures whose
heights this dataset does not have.

So every corridor here is labelled ``planning_heuristic_only``, ``safety_claim: false``
and ``requires_survey: true``, and the module refuses to emit any field that could read
as a clearance statement.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import geodesy
from .config import GisConfig
from .errors import SyntheticProvenanceError
from .geom import round_value, write_json
from .vertical import assert_no_safety_implication

ROAD_AXIS_BUCKET_DEG = 15.0

ALIGNMENT_NOTE = (
    "Derived from road and green-space alignment only, as a visual and planning "
    "heuristic. A drone is not safer for flying above a road: wires, traffic, trees and "
    "rooftops still apply, and this dataset has no vertical measurement for any of them."
)


@dataclass
class CorridorSet:
    features: list[dict[str, Any]] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)
    files: dict[str, Any] = field(default_factory=dict)


def build_corridors(
    config: GisConfig,
    *,
    roads: list[dict[str, Any]],
    parks: list[dict[str, Any]],
    buildings: list[dict[str, Any]] | None = None,
) -> CorridorSet:
    """Group road segments into candidate axes and label them honestly."""
    corridor_cfg = config.corridor
    tolerance_m = float(corridor_cfg.get("grouping_tolerance_m", 120.0))
    min_length_m = float(corridor_cfg.get("min_corridor_length_m", 400.0))
    max_corridors = int(corridor_cfg.get("max_corridors", 60))
    reference_lat = config.reference_latitude_deg

    park_points = _points_of(parks)
    groups: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
    for feature in roads:
        for part in _line_parts(feature):
            coords = part.get("coordinates") or []
            if len(coords) < 2:
                continue
            length = geodesy.length_m(coords)
            if length <= 0:
                continue
            start = (coords[0][1], coords[0][0])
            end = (coords[-1][1], coords[-1][0])
            axis = geodesy.bearing_deg(start, end)
            bucket = geodesy.orientation_bucket(axis, ROAD_AXIS_BUCKET_DEG)
            mid = (
                (coords[0][1] + coords[-1][1]) / 2.0,
                (coords[0][0] + coords[-1][0]) / 2.0,
            )
            east, north = geodesy.to_plane(mid, reference_lat)
            band = _perpendicular_band(east, north, axis, tolerance_m)
            groups[(bucket, band)].append(
                {
                    "part": part,
                    "length_m": length,
                    "axis_deg": round(axis, 2),
                    "mid": mid,
                    "props": feature.get("properties", {}),
                }
            )

    corridors: list[dict[str, Any]] = []
    for (bucket, band), segments in groups.items():
        total = sum(segment["length_m"] for segment in segments)
        if total < min_length_m:
            continue
        corridors.append(
            _corridor_feature(
                config,
                bucket=bucket,
                band=band,
                segments=segments,
                total=total,
                park_points=park_points,
                tolerance_m=tolerance_m,
                buildings_present=bool(buildings),
            )
        )

    corridors.sort(key=lambda feature: (-feature["properties"]["length_m"], feature["properties"]["corridor_id"]))
    truncated = len(corridors) > max_corridors
    corridors = corridors[:max_corridors]
    corridors.sort(key=lambda feature: feature["properties"]["corridor_id"])
    for feature in corridors:
        audit_corridor(feature)

    summary = {
        "method": corridor_cfg.get("method", "synthetic"),
        "basis": corridor_cfg.get("basis", "road and green-space heuristic"),
        "safety_claim": False,
        "planning_heuristic_only": True,
        "requires_survey": True,
        "authoritative": False,
        "corridors": len(corridors),
        "truncated_to_max": truncated,
        "grouping_tolerance_m": tolerance_m,
        "min_corridor_length_m": min_length_m,
        "road_classes": sorted({str(c) for c in corridor_cfg.get("road_classes", [])}),
        "note": ALIGNMENT_NOTE,
        "total_length_m": round(sum(f["properties"]["length_m"] for f in corridors), 1),
        "green_space_aligned": sum(
            1 for f in corridors if f["properties"]["green_space_adjacent"]
        ),
        "road_side_obstacle_density_available": bool(buildings),
        "what_this_does_not_prove": [
            "that any corridor has obstacle clearance",
            "that any corridor is permitted, or legal, or surveyed",
            "that flying above a road reduces risk",
            "that the wires crossing these roads are anywhere near the assumed height",
        ],
    }
    return CorridorSet(features=corridors, summary=summary)


def _corridor_feature(
    config: GisConfig,
    *,
    bucket: int,
    band: int,
    segments: list[dict[str, Any]],
    total: float,
    park_points: list[tuple[float, float]],
    tolerance_m: float,
    buildings_present: bool,
) -> dict[str, Any]:
    geometry = {
        "type": "MultiLineString",
        "coordinates": [segment["part"]["coordinates"] for segment in segments],
    }
    classes = sorted(
        {
            str(segment["props"].get("highway", ""))
            for segment in segments
            if segment["props"].get("highway")
        }
    )
    midpoint = segments[len(segments) // 2]["mid"]
    nearest_park_m = _nearest_distance_m(midpoint, park_points)
    provenances = [
        segment["props"].get("provenance", {}) for segment in segments if segment["props"]
    ]
    corridor_id = f"corridor-{bucket:03d}-{band:+05d}"
    properties: dict[str, Any] = {
        "corridor_id": corridor_id,
        "layer": "candidate_corridor",
        "method": config.corridor.get("method", "synthetic"),
        "basis": config.corridor.get("basis", "road and green-space heuristic"),
        "axis_bucket_deg": bucket * ROAD_AXIS_BUCKET_DEG,
        "segment_count": len(segments),
        "length_m": round(total, 1),
        "road_classes": classes,
        "green_space_adjacent": nearest_park_m is not None and nearest_park_m <= tolerance_m,
        "nearest_green_space_m": round(nearest_park_m, 1) if nearest_park_m is not None else None,
        "label": str(
            config.corridor.get(
                "corridor_label", "Synthetic corridor model (planning heuristic only)"
            )
        ),
        "planning_heuristic_only": True,
        "safety_claim": False,
        "requires_survey": True,
        "authoritative": False,
        "synthetic": True,
        "obstacle_source": "synthetic",
        "vertical_authority": "none",
        "altitude_reference": config.vertical.altitude_reference,
        "clearance_use": "forbidden",
        "road_above_note": ALIGNMENT_NOTE,
        "provenance": provenances[0] if provenances else {},
        "retrieval_window": sorted({str(p.get("retrieved_at", "")) for p in provenances})[:3],
    }
    if not buildings_present:
        properties["obstacle_context_missing"] = (
            "No obstacle layer was generated in this run, so this corridor is a bare "
            "alignment with nothing at all known about what is under or across it."
        )
    return {"type": "Feature", "geometry": round_value(geometry), "properties": properties}


def audit_corridor(feature: dict[str, Any]) -> None:
    """A corridor may not claim safety, and must say it needs a survey."""
    properties = feature.get("properties", {})
    identifier = str(properties.get("corridor_id", "?"))
    for key in ("safety_claim", "planning_heuristic_only", "requires_survey", "authoritative"):
        if key not in properties:
            raise SyntheticProvenanceError(f"corridor {identifier} is missing {key!r}")
    if properties["safety_claim"] is not False:
        raise SyntheticProvenanceError(f"corridor {identifier} asserts a safety claim")
    if properties["planning_heuristic_only"] is not True:
        raise SyntheticProvenanceError(f"corridor {identifier} is not labelled a heuristic")
    if properties["requires_survey"] is not True:
        raise SyntheticProvenanceError(f"corridor {identifier} claims a survey is unnecessary")
    if properties["authoritative"] is not False:
        raise SyntheticProvenanceError(f"corridor {identifier} presents itself as authoritative")
    assert_no_safety_implication(properties, identifier)


def _perpendicular_band(east_m: float, north_m: float, axis_deg: float, tolerance_m: float) -> int:
    """Offset of a segment from the corridor axis, quantised into a band index."""
    import math

    radians = math.radians(axis_deg)
    # Unit normal to the axis: (sin, -cos) of the bearing, in metres east/north.
    offset = east_m * math.cos(radians) - north_m * math.sin(radians)
    return int(round(offset / max(tolerance_m, 1.0)))


def _line_parts(feature: dict[str, Any]) -> list[dict[str, Any]]:
    """Every LineString of a feature, whether it arrived as one line or a clipped mix."""
    geometry = feature.get("geometry") or {}
    kind = geometry.get("type")
    if kind == "LineString":
        return [geometry]
    if kind == "MultiLineString":
        return [
            {"type": "LineString", "coordinates": part}
            for part in geometry.get("coordinates", [])
            if isinstance(part, list)
        ]
    if kind == "GeometryCollection":
        parts: list[dict[str, Any]] = []
        for member in geometry.get("geometries", []):
            parts.extend(_line_parts({"geometry": member}))
        return parts
    return []


def _points_of(features: list[dict[str, Any]]) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    for feature in features:
        geometry = feature.get("geometry") or {}
        kind = geometry.get("type", "")
        coordinates = geometry.get("coordinates")
        if kind == "Point" and isinstance(coordinates, list) and len(coordinates) >= 2:
            points.append((float(coordinates[1]), float(coordinates[0])))
    return points


def _nearest_distance_m(point: tuple[float, float], others: list[tuple[float, float]]) -> float | None:
    if not others:
        return None
    return min(geodesy.haversine_m(point, other) for other in others)


def write_corridors(corridors: CorridorSet, geojson_path: Path, summary_path: Path) -> dict[str, Any]:
    collection = {
        "type": "FeatureCollection",
        "name": "candidate_corridors",
        "authoritative": False,
        "safety_claim": False,
        "planning_heuristic_only": True,
        "note": ALIGNMENT_NOTE,
        "features": corridors.features,
    }
    files = {
        geojson_path.name: {"sha256": write_json(geojson_path, collection)},
        summary_path.name: {"sha256": write_json(summary_path, corridors.summary)},
    }
    return files
