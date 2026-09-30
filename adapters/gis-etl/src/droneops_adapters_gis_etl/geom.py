"""Geometry: Overpass element -> normalised GeoJSON, clipped and deterministically rounded.

Coordinate rounding to 7 decimals (~11 mm at the equator, ~9 mm in Chandigarh) happens
before hashing so a re-run of the same cached response produces byte-identical
artefacts. That is a provenance property, not cosmetics: a snapshot hash that drifts
between runs cannot be cited.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from shapely.geometry import MultiLineString, MultiPolygon, Polygon, mapping, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import polygonize, unary_union

from .hashing import file_sha256

COORD_DECIMALS = 7
#: A ring with fewer than these points is not a building, it is a query artefact.
MIN_RING_POINTS = 3


def round_value(value: Any, decimals: int = COORD_DECIMALS) -> Any:
    """Round every float in a nested JSON structure; recurse into lists and dicts."""
    if isinstance(value, float):
        return round(value, decimals)
    if isinstance(value, list):
        return [round_value(item, decimals) for item in value]
    if isinstance(value, dict):
        return {key: round_value(item, decimals) for key, item in value.items()}
    return value


def _point(lat: Any, lon: Any) -> dict[str, Any] | None:
    if lat is None or lon is None:
        return None
    return {"type": "Point", "coordinates": [round(float(lon), COORD_DECIMALS), round(float(lat), COORD_DECIMALS)]}


def _linestring(coords: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
    if len(coords) < MIN_RING_POINTS - 1:
        return None
    return {"type": "LineString", "coordinates": [[round(float(c["lon"]), COORD_DECIMALS), round(float(c["lat"]), COORD_DECIMALS)] for c in coords]}


def element_geometry(element: dict[str, Any]) -> dict[str, Any] | None:
    """GeoJSON geometry for one Overpass element, from whichever output mode was used.

    Priority: explicit ``geometry`` (``out geom``), then ``center`` (``out center``),
    then the node's own ``lat``/``lon``. A way retrieved with ``out body`` has no
    coordinates at all and returns ``None`` rather than a guessed position.
    """
    etype = element.get("type")
    if etype == "node":
        return _point(element.get("lat"), element.get("lon"))
    if etype in ("way", "relation"):
        raw = element.get("geometry")
        if isinstance(raw, list) and raw and isinstance(raw[0], dict):
            line = _linestring(raw)
            if etype == "relation" or line is None:
                return line
            if _is_closed(raw):
                return {"type": "Polygon", "coordinates": [_ring_coords(raw)]}
            return line
        center = element.get("center")
        if isinstance(center, dict):
            return _point(center.get("lat"), center.get("lon"))
    return None


def _is_closed(coords: Sequence[dict[str, Any]]) -> bool:
    if len(coords) < 4:
        return False
    first, last = coords[0], coords[-1]
    return abs(float(first["lat"]) - float(last["lat"])) < 1e-9 and abs(
        float(first["lon"]) - float(last["lon"])
    ) < 1e-9


def _ring_coords(coords: Sequence[dict[str, Any]]) -> list[list[float]]:
    ring = [
        [round(float(c["lon"]), COORD_DECIMALS), round(float(c["lat"]), COORD_DECIMALS)]
        for c in coords
    ]
    if ring[0] != ring[-1]:
        ring.append(list(ring[0]))
    return ring


def element_properties(element: dict[str, Any], *, layer: str) -> dict[str, Any]:
    """The normalised property bag: identity, tags and the classification this layer cares about."""
    tags = element.get("tags") or {}
    props: dict[str, Any] = {
        "layer": layer,
        "osm_type": element.get("type"),
        "osm_id": element.get("id"),
        "osm_kind": layer,
        "tags": {str(k): str(v) for k, v in sorted(tags.items())},
    }
    for key in ("highway", "building", "landuse", "leisure", "power", "man_made", "aeroway", "natural", "waterway", "amenity", "healthcare", "shop", "office", "craft", "name", "height", "building:levels", "layer", "voltage", "circuits"):
        if key in tags:
            props[key] = str(tags[key])
    return props


# ------------------------------------------------------------------------ boundary


def boundary_polygon(elements: Iterable[dict[str, Any]], *, expected_name: str = "") -> BaseGeometry:
    """Rebuild the administrative polygon from relation member geometry.

    Overpass returns members in no guaranteed order and a district boundary can be
    multipart, so the rings are closed, polygonised and unioned. If nothing can be
    polygonised the caller gets an empty geometry and must fail loudly - a missing
    boundary must not turn into "clip against the bbox" and be presented as a mask.
    """
    rings: list[Sequence[dict[str, Any]]] = []
    for element in elements:
        raw = element.get("geometry")
        if isinstance(raw, list) and len(raw) >= 4:
            rings.append(raw)
    if not rings:
        return Polygon()
    coordinates = [
        [
            [round(float(c["lon"]), COORD_DECIMALS), round(float(c["lat"]), COORD_DECIMALS)]
            for c in ring
        ]
        for ring in rings
    ]
    closed = []
    for ring in coordinates:
        if ring[0] != ring[-1]:
            ring = [*ring, list(ring[0])]
        closed.append(ring)
    polygons: list[BaseGeometry] = []
    for ring in closed:
        try:
            candidate = Polygon(ring)
        except Exception:
            continue
        if not candidate.is_valid:
            candidate = candidate.buffer(0)
        if not candidate.is_empty and isinstance(candidate, Polygon):
            polygons.append(candidate)
    if not polygons:
        # Ring order was broken across members: fall back to polygonising the lines.
        lines = []
        for ring in closed:
            try:
                lines.append(MultiLineString([ring]))
            except Exception:
                continue
        if not lines:
            return Polygon()
        unioned = unary_union(lines)
        polygons = list(polygonize(unioned))
    if not polygons:
        return Polygon()
    merged = unary_union(polygons)
    if isinstance(merged, Polygon):
        result: BaseGeometry = merged
    elif isinstance(merged, MultiPolygon):
        result = merged
    else:  # pragma: no cover - GeometryCollection
        result = unary_union([g for g in merged.geoms if isinstance(g, (Polygon, MultiPolygon))])
    if expected_name:
        names = {
            str((element.get("tags") or {}).get("name", ""))
            for element in elements
            if element.get("type") == "relation"
        }
        if names and expected_name not in names:
            raise ValueError(
                f"boundary identity check failed: expected {expected_name!r}, relation "
                f"names {sorted(names)}"
            )
    return result


def parse_boundary_geojson(geojson: dict[str, Any]) -> BaseGeometry:
    """A user-supplied boundary GeoJSON (Polygon / MultiPolygon / Feature)."""
    if geojson.get("type") == "Feature":
        geojson = geojson["geometry"]
    if geojson.get("type") == "FeatureCollection":
        parts = [shape(f["geometry"]) for f in geojson.get("features", []) if f.get("geometry")]
        return unary_union(parts) if parts else Polygon()
    return shape(geojson)


# --------------------------------------------------------------------- collections


def make_feature(geometry: dict[str, Any], properties: dict[str, Any]) -> dict[str, Any]:
    return {"type": "Feature", "geometry": geometry, "properties": properties}


def feature_collection(features: Sequence[dict[str, Any]], *, name: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "FeatureCollection",
        "name": name,
        "features": list(features),
    }
    payload.update(extra or {})
    return payload


def clip_feature(
    feature: dict[str, Any], mask: BaseGeometry | None
) -> dict[str, Any] | None:
    """Intersect a feature with the boundary mask, keeping only what survives.

    A geometry that lands entirely outside the mask is *dropped*, not clipped to an
    empty shape: an empty geometry in a processed layer reads as "this obstacle exists
    and is zero-sized", which is a different and more misleading claim.
    """
    if mask is None or mask.is_empty:
        return feature
    geometry = feature.get("geometry")
    if not geometry:
        return None
    try:
        source = shape(geometry)
    except Exception:
        return None
    try:
        clipped = source.intersection(mask)
    except Exception:
        try:
            clipped = source.buffer(0).intersection(mask.buffer(0))
        except Exception:
            return None
    if clipped.is_empty:
        return None
    feature = dict(feature)
    feature["geometry"] = round_value(mapping(clipped))
    return feature


def write_json(path: Path, payload: Any) -> str:
    """Write sorted JSON and return its sha256 - the manifest records the file, not a promise."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    _atomic_write(path, text.encode("utf-8"))
    return file_sha256(path)


def write_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> str:
    """JSON Lines for the big layers: appendable, diffable, and hashable as a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(
        json.dumps(record, sort_keys=True, ensure_ascii=True) + "\n" for record in records
    )
    _atomic_write(path, text.encode("utf-8"))
    return file_sha256(path)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _atomic_write(path: Path, payload: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(payload)
    tmp.replace(path)
