"""Feature extraction: Overpass response -> normalised, clipped, provenance-complete layers.

What comes out of here is *horizontal* and measured. Nothing in this module assigns a
height; that is the synthetic vertical model's job and it is deliberately a separate
file, because the moment measured footprint and assumed height are produced by the same
function nobody can tell which half of a record is evidence.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from shapely.geometry.base import BaseGeometry

from .config import GisConfig
from .errors import ExtractionLimitError, GisEtlError
from .geom import (
    clip_feature,
    element_geometry,
    element_properties,
    make_feature,
    round_value,
    write_json,
    write_jsonl,
)
from .overpass import OverpassClient
from .provenance import (
    OSM_ATTRIBUTION,
    OSM_LICENSE,
    SOURCE_MODE_CACHE,
    SOURCE_MODE_LIVE,
    RetrievalProvenance,
)
from .queries import boundary_query, boundary_tags_query, layer_query

GEOJSON_MAX_FEATURES = 5000
_HEIGHT_PATTERN = re.compile(r"(-?\d+(?:[.,]\d+)?)")
_TAG_ORDER = (
    "osm_type",
    "osm_id",
    "layer",
    "highway",
    "cycleway",
    "building",
    "landuse",
    "leisure",
    "natural",
    "waterway",
    "power",
    "voltage",
    "circuits",
    "man_made",
    "tower_type",
    "communication",
    "aeroway",
    "amenity",
    "healthcare",
    "shop",
    "office",
    "craft",
    "name",
    "height",
    "building:levels",
    "layer_ref",
)


@dataclass
class LayerOutcome:
    """What was actually obtained for one layer, including what was not."""

    layer: str
    features: int = 0
    dropped_no_geometry: int = 0
    dropped_outside_boundary: int = 0
    duplicates_removed: int = 0
    truncated: bool = False
    chunks_requested: int = 0
    chunks_live: int = 0
    chunks_cached: int = 0
    chunks_failed: int = 0
    failures: list[dict[str, Any]] = field(default_factory=list)
    files: dict[str, str] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)
    degraded: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "layer": self.layer,
            "features": self.features,
            "dropped_no_geometry": self.dropped_no_geometry,
            "dropped_outside_boundary": self.dropped_outside_boundary,
            "duplicates_removed": self.duplicates_removed,
            "truncated": self.truncated,
            "chunks": {
                "requested": self.chunks_requested,
                "live": self.chunks_live,
                "cache": self.chunks_cached,
                "failed": self.chunks_failed,
            },
            "degraded": self.degraded,
            "failures": self.failures,
            "files": self.files,
            "provenance": self.provenance,
        }


class LayerExtractor:
    """Fetch and normalise one layer at a time, chunked, into the processed directory."""

    def __init__(
        self,
        config: GisConfig,
        client: OverpassClient,
        *,
        geojson_max_features: int = GEOJSON_MAX_FEATURES,
    ) -> None:
        self.config = config
        self.client = client
        self.geojson_max = geojson_max_features
        self.layers_dir = config.processed_dir / "layers"

    # ------------------------------------------------------------------ boundary
    def run_boundary(self) -> tuple[RetrievalProvenance, BaseGeometry, dict[str, Any]]:
        """Retrieve relation 1942809, verify its identity, and write it as the display mask."""
        relation = self.config.boundary_relation_id
        tags_query = boundary_tags_query(relation)
        tags_result = self.client.fetch(tags_query, label=f"boundary:tags:{relation}")
        tags_payload = tags_result.json()
        elements = tags_payload.get("elements") or []
        if not elements:
            raise GisEtlError(
                f"administrative relation {relation} returned no elements. Refusing to "
                "publish a boundary we cannot identify: the alternative is masking the "
                "map with an unnamed polygon and calling it Chandigarh."
            )
        tags = elements[0].get("tags") or {}
        name = str(tags.get("name", ""))
        expected = self.config.expected_boundary_name
        if expected and name != expected:
            raise GisEtlError(
                f"relation {relation} is named {name!r}, config expects {expected!r}. "
                "Boundary identity is a provenance claim; it is not overridden by a retry."
            )

        geom_query = boundary_query(relation, timeout_s=int(self.config.overpass.request_timeout_s))
        geom_result = self.client.fetch(geom_query, label=f"boundary:geom:{relation}")
        from .geom import boundary_polygon

        payload = geom_result.json()
        members = payload.get("elements") or []
        polygon = boundary_polygon(members)
        if polygon.is_empty:
            raise GisEtlError(
                f"relation {relation} returned {len(members)} elements but no ring could "
                "be polygonised. Clipping would have to fall back to the bounding box, "
                "and a bbox is not an administrative boundary."
            )

        provenance = self._provenance(geom_result, note="administrative boundary geometry")
        feature = make_feature(
            round_value(_geometry_of(polygon)),
            {
                "osm_type": "relation",
                "osm_id": relation,
                "layer": "boundary",
                "name": name,
                "admin_level": str(tags.get("admin_level", "")),
                "iso3166_2": str(tags.get("ISO3166-2", "")),
                "wikidata": str(tags.get("wikidata", "")),
                "boundary": str(tags.get("boundary", "")),
                "role": "display_mask_only",
                "flight_legality_evidence": False,
                "note": (
                    "Administrative boundary used as a display mask. It is not proof that "
                    "any point inside it is legal to fly (spec 12 section 1)."
                ),
                "provenance": provenance.as_dict(),
            },
        )
        collection = {
            "type": "FeatureCollection",
            "name": "boundary",
            "authoritative": False,
            "features": [feature],
        }
        path = self.layers_dir / "boundary.geojson"
        digest = write_json(path, collection)
        files = {_relative(path, self.config.root): {"sha256": digest, "features": 1, "format": "geojson"}}
        detail = {
            "relation_id": relation,
            "name": name,
            "admin_level": str(tags.get("admin_level", "")),
            "iso3166_2": str(tags.get("ISO3166-2", "")),
            "wikidata": str(tags.get("wikidata", "")),
            "geometry_valid": bool(polygon.is_valid),
            "area_km2": round(float(polygon.area) * 111.32 * 110.574, 2),
            "bounds": list(polygon.bounds),
            "identity_provenance": self._provenance(tags_result, note="boundary tags").as_dict(),
        }
        return provenance, polygon, {"files": files, "detail": detail}

    # --------------------------------------------------------------------- layers
    def run_layer(self, name: str, mask: BaseGeometry | None) -> LayerOutcome:
        spec = self.config.layers.get(name)
        if spec is None or not spec.enabled:
            raise GisEtlError(f"layer {name!r} is not enabled in the config")
        chunks = self.config.service_bbox.split(spec.chunk_grid, spec.chunk_grid)
        outcome = LayerOutcome(layer=name)
        collected: dict[tuple[str, int], dict[str, Any]] = {}
        for index, chunk in enumerate(chunks):
            query = layer_query(
                name,
                chunk,
                output=spec.output,
                timeout_s=int(self.config.overpass.request_timeout_s),
                config=self.config,
            )
            outcome.chunks_requested += 1
            label = f"layer:{name}:chunk{index}"
            try:
                result = self.client.fetch(query, label=label)
            except GisEtlError as exc:
                outcome.chunks_failed += 1
                outcome.failures.append(
                    {
                        "chunk": index,
                        "bbox": list(chunk.as_tuple()),
                        "error_type": type(exc).__name__,
                        "error": str(exc)[:700],
                    }
                )
                continue
            if result.source_mode == SOURCE_MODE_LIVE:
                outcome.chunks_live += 1
            elif result.source_mode == SOURCE_MODE_CACHE:
                outcome.chunks_cached += 1
                outcome.degraded = True
            provenance = self._provenance(result, note=f"layer {name} chunk {index}")
            for element in (result.json().get("elements") or []):
                feature = self._feature(element, name, provenance)
                if feature is None:
                    outcome.dropped_no_geometry += 1
                    continue
                key = (str(feature["properties"]["osm_type"]), int(feature["properties"]["osm_id"]))
                if key in collected:
                    outcome.duplicates_removed += 1
                    continue
                clipped = clip_feature(feature, mask)
                if clipped is None:
                    outcome.dropped_outside_boundary += 1
                    continue
                clipped["properties"]["inside_boundary"] = True
                collected[key] = clipped

        ordered = [collected[key] for key in sorted(collected)]
        if len(ordered) > spec.max_features:
            if self.config.raw.get("layers", {}).get(name, {}).get("allow_truncation", True) is False:
                raise ExtractionLimitError(
                    f"layer {name} returned {len(ordered)} features over the cap "
                    f"{spec.max_features} and truncation is disallowed in config"
                )
            ordered = ordered[: spec.max_features]
            outcome.truncated = True
        outcome.features = len(ordered)
        outcome.files = self._write_layer(name, ordered)
        outcome.provenance = {
            "source": "OpenStreetMap via Overpass API",
            "licence": OSM_LICENSE,
            "attribution": OSM_ATTRIBUTION,
            "output_mode": spec.output,
            "chunk_grid": spec.chunk_grid,
            "bbox_service": list(self.config.service_bbox.as_tuple()),
            "clip_mask": "administrative boundary relation geometry",
            "per_feature_provenance": True,
            "retrieval_windows": sorted(
                {
                    feature["properties"]["provenance"]["retrieved_at"]
                    for feature in ordered
                    if "provenance" in feature["properties"]
                }
            )[-4:],
            "last_provenance": provenance_dict(ordered),
        }
        return outcome

    def _feature(
        self, element: dict[str, Any], name: str, provenance: RetrievalProvenance
    ) -> dict[str, Any] | None:
        geometry = element_geometry(element)
        if geometry is None:
            return None
        properties = element_properties(element, layer=name)
        properties["height_tag_m"] = parse_height_m(element.get("tags", {}).get("height"))
        properties["levels_tag"] = parse_levels(element.get("tags", {}).get("building:levels"))
        properties["height_tag_raw"] = str(element.get("tags", {}).get("height", "") or "")
        properties["levels_tag_raw"] = str(element.get("tags", {}).get("building:levels", "") or "")
        properties["provenance"] = provenance.as_dict()
        return make_feature(round_value(geometry), _order(properties))

    def _write_layer(self, name: str, features: Iterable[dict[str, Any]]) -> dict[str, Any]:
        materialised = list(features)
        jsonl_path = self.layers_dir / f"{name}.jsonl"
        files: dict[str, Any] = {_relative(jsonl_path, self.config.root): {
            "sha256": write_jsonl(jsonl_path, materialised),
            "features": len(materialised),
            "format": "jsonl",
        }}
        if len(materialised) <= self.geojson_max:
            geojson_path = self.layers_dir / f"{name}.geojson"
            collection = {
                "type": "FeatureCollection",
                "name": name,
                "authoritative": False,
                "features": materialised,
            }
            files[_relative(geojson_path, self.config.root)] = {
                "sha256": write_json(geojson_path, collection),
                "features": len(materialised),
                "format": "geojson",
            }
        return files

    def _provenance(self, result: Any, *, note: str) -> RetrievalProvenance:
        record = result.record
        return RetrievalProvenance(
            source=f"OpenStreetMap via Overpass API ({record.endpoint})",
            retrieved_at=datetime.fromisoformat(record.retrieved_at),
            expires_at=None,
            freshness=_freshness(record.retrieved_at),
            endpoint=record.endpoint,
            query_sha256=record.query_sha256,
            query_text=record.query_text,
            response_sha256=record.response_sha256,
            raw_path=_relative(record.path, self.config.root),
            source_mode=record.source_mode,
            licence=OSM_LICENSE,
            attribution=OSM_ATTRIBUTION,
            replication_timestamp=record.replication_timestamp,
            degraded=result.degraded,
            note=note,
        )


def provenance_dict(features: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Provenance of the last chunk retrieved: a convenience, never an authority."""
    return features[-1]["properties"]["provenance"] if features else {}


def _order(properties: dict[str, Any]) -> dict[str, Any]:
    """Sorted key order so the same input always produces the same bytes."""
    ordered: dict[str, Any] = {}
    for key in _TAG_ORDER:
        if key in properties:
            ordered[key] = properties[key]
    for key in sorted(properties):
        ordered.setdefault(key, properties[key])
    return ordered


def parse_height_m(raw: Any) -> float | None:
    """Parse an OSM ``height`` tag (metres, with unit noise) into float metres.

    Returns None for anything not confidently a length: an assumed height is allowed
    (and flagged), a *mis-parsed* height would be presented as measured data.
    """
    if raw is None:
        return None
    text = str(raw).strip().lower()
    if not text:
        return None
    match = _HEIGHT_PATTERN.search(text.replace(",", "."))
    if not match:
        return None
    try:
        value = float(match.group(1))
    except ValueError:
        return None
    if "ft" in text or "'" in text or "feet" in text:
        return round(value * 0.3048, 3)
    if value > 1000 or value < 0.2:
        return None
    return round(value, 3)


def parse_levels(raw: Any) -> float | None:
    """Parse ``building:levels`` (may be ``2;4``, ``3.5``, ``-1`` for a basement)."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    first = re.split(r"[;:,\s]", text)[0]
    try:
        value = float(first)
    except ValueError:
        return None
    if value < 0:
        return None
    return round(value, 2)


def _geometry_of(geometry: BaseGeometry) -> dict[str, Any]:
    from shapely.geometry import mapping

    return mapping(geometry)


def _freshness(retrieved_at: str) -> Any:
    from droneops_contracts import Freshness

    try:
        stamp = datetime.fromisoformat(retrieved_at)
    except ValueError:
        return Freshness.UNKNOWN
    from .provenance import utc_now

    age = (utc_now() - stamp).total_seconds()
    if age < 0:
        return Freshness.UNKNOWN
    if age <= 86_400:
        return Freshness.FRESH
    if age <= 30 * 86_400:
        return Freshness.AGING
    return Freshness.STALE


def _relative(path: str | Path, root: Path) -> str:
    candidate = Path(path)
    try:
        return candidate.relative_to(root).as_posix()
    except ValueError:
        return candidate.as_posix()
