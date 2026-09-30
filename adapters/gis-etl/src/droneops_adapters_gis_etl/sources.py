"""The Geofabrik path: download or accept a local extract, clip it, record everything.

Bulk work belongs in a local extract, not in repeated public Overpass queries (spec 12
section 3). Two things follow from that and are implemented here:

1. a **local path** is a first-class input. A 300 MB India extract will not reliably
   download inside a build, so the operator is expected to fetch it once, out of band,
   and point the adapter at it. ``--extract`` does that, and the README spells out the
   URL, the licence and the clipping command.
2. an extract's **identity is recorded before its features are used**: sha256 of the
   file, the extract date from its own header, the bbox it actually covers, the source
   URL and the licence. A layer without those cannot enter the manifest.
"""

from __future__ import annotations

import hashlib
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

import httpx
from droneops_contracts import Freshness

from .errors import GisEtlError
from .hashing import file_sha256, prefixed
from .geom import clip_feature, round_value
from .provenance import OSM_ATTRIBUTION, OSM_LICENSE, SOURCE_MODE_LOCAL_FILE, RetrievalProvenance
from .extract import parse_height_m, parse_levels
from .readers import (
    PbfHeader,
    ShapefileReader,
    read_geojson_features,
    read_jsonl_features,
    read_pbf_header,
)

GEOFABRIK_BASE = "https://download.geofabrik.de/asia/"
#: A download that cannot finish is normal. This cap keeps a build from hanging on it.
DEFAULT_MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024

PBF_FEATURE_GUIDANCE = (
    "The .osm.pbf feature-data blocks are not decoded here. Reading them means the whole "
    "of osmformat.proto including dense-node delta-delta tag streams, and a partly correct "
    "tag decoder would mis-assign heights - the one error this dataset cannot afford. "
    "Clip and export instead, then hand the adapter the result: "
    "osmium extract -b south,west,north,east india-latest.osm.pbf -o chandigarh.osm.pbf "
    "&& osmium export chandigarh.osm.pbf -f geojson -o chandigarh.geojson "
    "--overwrite"
)


@dataclass
class ExtractInfo:
    """Everything the manifest needs to know about an extract file."""

    path: str
    format: str
    sha256: str
    size_bytes: int
    extract_date: str
    source: str
    licence: str
    attribution: str
    retrieval_method: str
    bbox: list[float] | None = None
    writing_program: str = ""
    pbf: dict[str, Any] = field(default_factory=dict)
    feature_count: int = 0
    features_decodable: bool = True
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def detect_format(path: Path) -> str:
    name = path.name.lower()
    if name.endswith(".osm.pbf") or name.endswith(".pbf"):
        return "osm.pbf"
    if name.endswith(".shp.zip"):
        return "shp.zip"
    if name.endswith(".shp"):
        return "shp"
    if name.endswith(".geojson"):
        return "geojson"
    if name.endswith(".json"):
        return "geojson"
    if name.endswith((".jsonl", ".ndjson")):
        return "jsonl"
    raise GisEtlError(
        f"{path}: unsupported extract format. Accepted: .osm.pbf (metadata only), "
        ".shp (+.dbf), .shp.zip, .geojson/.json, .jsonl"
    )


def inspect_extract(
    path: Path,
    *,
    source: str = "",
    retrieval_method: str = SOURCE_MODE_LOCAL_FILE,
    licence: str = OSM_LICENSE,
    attribution: str = OSM_ATTRIBUTION,
) -> ExtractInfo:
    """Hash and identify an extract before anything reads its features."""
    path = Path(path)
    if not path.is_file():
        raise GisEtlError(f"extract not found: {path}")
    fmt = detect_format(path)
    info = ExtractInfo(
        path=str(path).replace("\\", "/"),
        format=fmt,
        sha256=file_sha256(path),
        size_bytes=path.stat().st_size,
        extract_date="",
        source=source or ("user-supplied local extract" if retrieval_method == SOURCE_MODE_LOCAL_FILE else retrieval_method),
        licence=licence,
        attribution=attribution,
        retrieval_method=retrieval_method,
    )
    if fmt == "osm.pbf":
        header: PbfHeader = read_pbf_header(path)
        info.extract_date = header.extract_date
        info.bbox = list(header.bbox) if header.bbox else None
        info.writing_program = header.writing_program
        info.pbf = header.as_dict()
        info.features_decodable = False
        info.note = PBF_FEATURE_GUIDANCE
    return info


def ingest_extract(path: Path, extract: ExtractInfo, *, layer: str) -> list[dict[str, Any]]:
    """Read features out of a supported local extract, clipped by the caller later.

    The provenance attached to each feature is the *file's*, including its sha256: a
    feature from ``india-latest.osm.pbf`` dated 2026-09-27 is a different claim from one
    retrieved live from Overpass today, and the difference must survive into the record.
    """
    if not extract.features_decodable:
        raise GisEtlError(f"{path}: {extract.note}")
    provenance = RetrievalProvenance(
        source=f"local extract {Path(extract.path).name} ({extract.format})",
        retrieved_at=datetime.fromisoformat(extract.extract_date)
        if extract.extract_date
        else datetime.now().astimezone().replace(microsecond=0),
        freshness=Freshness.UNKNOWN,  # a file's age is its extract date, not a retrieval TTL
        endpoint="",
        query_sha256=extract.sha256.removeprefix("sha256:"),
        query_text="",
        response_sha256=extract.sha256,
        raw_path=extract.path,
        source_mode=SOURCE_MODE_LOCAL_FILE,
        licence=extract.licence,
        attribution=extract.attribution,
        replication_timestamp=extract.extract_date,
        note=f"ingested from a user-supplied local extract, layer {layer}",
    ).as_dict()
    raw = _read_features(Path(extract.path), extract.format)
    features: list[dict[str, Any]] = []
    for index, feature in enumerate(raw):
        properties = dict(feature.get("properties") or {})
        properties.setdefault("layer", layer)
        properties["osm_type"] = properties.get("osm_type") or "extract"
        properties["osm_id"] = properties.get("osm_id") or index
        properties["provenance"] = provenance
        # Shapefile attribute names come from whoever exported them; both the OSM tag
        # spelling and the Geofabrik upper-case spelling are read, and only these two
        # known keys are ever interpreted as a vertical value.
        properties["height_tag_m"] = parse_height_m(
            properties.get("height") or properties.get("HEIGHT")
        )
        properties["levels_tag"] = parse_levels(
            properties.get("building:levels") or properties.get("BLDG_LVL")
        )
        features.append(
            {
                "type": "Feature",
                "geometry": round_value(feature.get("geometry")),
                "properties": properties,
            }
        )
    return features


def _read_features(path: Path, fmt: str) -> Iterator[dict[str, Any]]:
    if fmt == "geojson":
        return iter(read_geojson_features(path))
    if fmt == "jsonl":
        return iter(read_jsonl_features(path))
    if fmt == "shp":
        return iter(_shapefile_features(ShapefileReader(path).read()))
    if fmt == "shp.zip":
        return iter(_read_shp_zip(path))
    raise GisEtlError(f"{path}: cannot read features from format {fmt!r}")


def _read_shp_zip(path: Path) -> list[dict[str, Any]]:
    """Geofabrik publishes one zip of per-layer shapefiles; unpack to a cache dir first."""
    workdir = path.with_suffix("")
    workdir.mkdir(exist_ok=True)
    layers: list[dict[str, Any]] = []
    with zipfile.ZipFile(path) as archive:
        archive.extractall(workdir)
    for shp in sorted(workdir.rglob("*.shp")):
        for feature in _shapefile_features(ShapefileReader(shp).read()):
            properties = feature.setdefault("properties", {})
            properties["extract_layer"] = shp.stem
            layers.append(feature)
    return layers


def _shapefile_features(records: list[Any]) -> list[dict[str, Any]]:
    features: list[dict[str, Any]] = []
    for index, record in enumerate(records):
        if record.geometry is None:
            continue
        properties = dict(record.attributes)
        parsed = parse_height_m(properties.get("OSM_ID"))
        properties["osm_id"] = int(parsed) if parsed is not None else index
        properties["osm_type"] = str(properties.get("OSM_TYPE") or "way")
        features.append(
            {"type": "Feature", "geometry": record.geometry, "properties": properties}
        )
    return features


def clip_features_to_mask(
    features: list[dict[str, Any]], mask: Any
) -> tuple[list[dict[str, Any]], int]:
    """Drop what falls outside the boundary; keep what survives the intersection."""
    kept: list[dict[str, Any]] = []
    dropped = 0
    for feature in features:
        clipped = clip_feature(feature, mask)
        if clipped is None:
            dropped += 1
            continue
        clipped["properties"]["inside_boundary"] = True
        kept.append(clipped)
    return kept, dropped


def download_extract(
    url: str,
    dest_dir: Path,
    *,
    user_agent: str,
    max_bytes: int = DEFAULT_MAX_DOWNLOAD_BYTES,
    allow_download: bool = True,
    timeout_s: float = 120.0,
) -> ExtractInfo:
    """Stream one file into ``data/raw/geofabrik`` and return its identity.

    Deliberately not a resume-capable downloader and not a retry loop: a big regional
    extract is a one-time, out-of-band task, and pretending the adapter will finish it
    inside a build is how a build gets stuck. Callers may pass ``allow_download=False``
    to refuse network use entirely and use ``--extract`` with a local path.
    """
    if not allow_download:
        raise GisEtlError(
            f"download refused: {url}\n"
            "This run is configured not to pull a bulk extract. Download it yourself, "
            "then pass the local path: --extract /path/to/chandigarh.osm.pbf. "
            f"Geofabrik index: {GEOFABRIK_BASE}"
        )
    if not user_agent or "DroneOps" not in user_agent:
        raise GisEtlError("a bulk download requires the project-identifying user agent")
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = url.rsplit("/", 1)[-1] or "extract.bin"
    target = dest_dir / name
    partial = target.with_suffix(target.suffix + ".part")
    if target.is_file():
        info = inspect_extract(target, source=url, retrieval_method="cached download")
        info.note = (f"{info.note + ' | ' if info.note else ''}file already present, not re-downloaded")
        return info
    digest = hashlib.sha256()
    received = 0
    try:
        with httpx.stream(
            "GET", url, headers={"User-Agent": user_agent}, timeout=timeout_s, follow_redirects=True
        ) as response:
            response.raise_for_status()
            with partial.open("wb") as handle:
                for chunk in response.iter_bytes():
                    received += len(chunk)
                    if received > max_bytes:
                        raise GisEtlError(
                            f"download aborted at the configured cap of {max_bytes} bytes "
                            f"({url}). The partial file is kept at {partial} and is NOT "
                            "recorded as an extract: a truncated PBF has a valid header "
                            "and missing data, which is the worst possible combination."
                        )
                    digest.update(chunk)
                    handle.write(chunk)
    except httpx.HTTPError as exc:
        raise GisEtlError(
            f"download failed for {url}: {type(exc).__name__}: {exc}. No partial file is "
            f"recorded as an extract; use --extract with a local copy instead."
        ) from exc
    partial.replace(target)
    info = inspect_extract(
        target, source=url, retrieval_method="downloaded from Geofabrik"
    )
    info.note = (
        f"{info.note + ' | ' if info.note else ''}downloaded {received} bytes, "
        f"sha256 {prefixed(digest.hexdigest())}"
    )
    return info
