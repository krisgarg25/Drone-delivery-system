"""The pipeline: retrieve, extract, generate, prove. One run, one record.

Order matters and is not an optimisation:

    boundary -> mask every layer -> layers -> obstacles -> corridors -> coverage -> manifest

The boundary is resolved first because every subsequent artefact is clipped to it. A
run that cannot establish the boundary stops there rather than producing a bbox-shaped
"Chandigarh" that nobody can attribute to a source.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import GisConfig, default_config_path, load_config
from .cache import RawCache
from .corridor import CorridorSet, build_corridors, write_corridors
from .coverage import CoverageReport, run_coverage
from .errors import GisEtlError
from .extract import LayerExtractor
from .geom import read_jsonl, write_json
from .hashing import file_sha256
from .manifest import RunRecord, build_manifest, write_manifest
from .overpass import OverpassClient
from .provenance import utc_now
from .sources import ExtractInfo, ingest_extract, inspect_extract, clip_features_to_mask
from .vertical import build_obstacles

COVERAGE_FILE = "coverage_report.json"
RUN_RECORD_FILE = "run_record.json"
MANIFEST_FILE = "manifest.yaml"


class ChandigarhEtl:
    """State of one ETL run against one config."""

    def __init__(
        self,
        config: GisConfig,
        *,
        live: bool = True,
        transport: Any | None = None,
        sleeper: Any = None,
    ) -> None:
        self.config = config
        self.cache = RawCache(config.raw_dir / "overpass", namespace="")
        self.client = OverpassClient(
            config.overpass,
            self.cache,
            live=live,
            transport=transport,
            sleeper=sleeper or (lambda seconds: None if not live else _sleep(seconds)),
        )
        self.extractor = LayerExtractor(config, self.client)
        self.processed = config.processed_dir
        self.record = RunRecord(started_at=utc_now(), finished_at=utc_now())
        self.mask: Any = None
        self._layers: dict[str, list[dict[str, Any]]] = {}

    # ------------------------------------------------------------------- stages
    def run_boundary(self) -> dict[str, Any]:
        provenance, polygon, extra = self.extractor.run_boundary()
        self.mask = polygon
        boundary_file = next(iter(extra["files"]))
        self.record.boundary = {
            "hash": extra["files"][boundary_file]["sha256"],
            "file": boundary_file,
            "name": extra["detail"]["name"],
            "admin_level": extra["detail"]["admin_level"],
            "iso3166_2": extra["detail"]["iso3166_2"],
            "wikidata": extra["detail"]["wikidata"],
            "licence": provenance.licence,
            "attribution": provenance.attribution,
            "retrieved_at": provenance.retrieved_at.isoformat(),
            "endpoint": provenance.endpoint,
            "query_sha256": provenance.query_sha256,
            "geometry_valid": extra["detail"]["geometry_valid"],
            "area_km2": extra["detail"]["area_km2"],
            "bounds": extra["detail"]["bounds"],
        }
        self._count_component(provenance.source_mode)
        self.record.layers["boundary"] = {"files": extra["files"], "features": 1, "chunks": {}}
        return self.record.boundary

    def run_layers(self, names: list[str] | None = None) -> dict[str, Any]:
        targets = names or list(sorted(self.config.layers))
        for name in targets:
            if name == "boundary" or name not in self.config.layers:
                continue
            outcome = self.extractor.run_layer(name, self.mask)
            self.record.layers[name] = outcome.as_dict()
            self.record.live_components += outcome.chunks_live
            self.record.cached_components += outcome.chunks_cached
            self.record.failed_components += outcome.chunks_failed
            if outcome.degraded:
                self.record.degradations.append(
                    f"layer {name}: served partly from cache; retrieval date is the cache's"
                )
            for failure in outcome.failures:
                self.record.degradations.append(
                    f"layer {name} chunk {failure['chunk']}: {failure['error_type']}"
                )
            self._layers[name] = read_jsonl(self.processed / "layers" / f"{name}.jsonl")
        return self.record.layers

    def run_obstacles(self) -> dict[str, Any]:
        obstacles = build_obstacles(
            self.config,
            buildings=self._layers.get("buildings", []),
            power_lines=self._layers.get("power_lines", []),
            towers=self._layers.get("towers", []),
            trees=self._layers.get("trees", []),
        )
        summary = self.processed / "obstacles"
        summary.mkdir(parents=True, exist_ok=True)
        jsonl = summary / "obstacles.jsonl"
        summary_file = summary / "obstacles_summary.json"
        from .vertical import write_obstacles

        files = write_obstacles(obstacles, jsonl, summary_file)
        self.record.obstacles = {
            **obstacles.summary,
            "hash": files["obstacles_jsonl"]["sha256"],
            "summary_hash": files["obstacles_summary"]["sha256"],
            "files": {
                self._rel(jsonl): files["obstacles_jsonl"],
                self._rel(summary_file): files["obstacles_summary"],
            },
        }
        return self.record.obstacles

    def run_corridors(self) -> dict[str, Any]:
        corridors = build_corridors(
            self.config,
            roads=self._layers.get("roads", []),
            parks=self._layers.get("parks", []),
            buildings=self._layers.get("buildings", []),
        )
        geojson = self.processed / "corridors" / "candidate_corridors.geojson"
        summary_file = self.processed / "corridors" / "corridors_summary.json"
        files = write_corridors(corridors, geojson, summary_file)
        self.record.corridors = {
            **corridors.summary,
            "hash": files[geojson.name]["sha256"],
            "files": {
                self._rel(geojson): files[geojson.name],
                self._rel(summary_file): files[summary_file.name],
            },
        }
        return self.record.corridors

    def run_coverage(self) -> dict[str, Any]:
        report: CoverageReport = run_coverage(self.client, self.config)
        path = self.processed / COVERAGE_FILE
        digest = report.write(path)
        self.record.coverage = {
            **report.as_dict(),
            "hash": digest,
            "file": self._rel(path),
            "baseline_retrieved_at": str(self.config.baseline.get("retrieved_at", "")),
        }
        self.record.live_components += report.live_metrics
        self.record.cached_components += report.cached_metrics
        self.record.failed_components += len(report.failures)
        if not report.claim_current:
            self.record.degradations.append(
                "coverage: the vertical-coverage claim is not fully re-measured in this run"
            )
        return self.record.coverage

    def run_extract(self, info: ExtractInfo, *, layer: str = "extract") -> dict[str, Any]:
        features = ingest_extract(Path(info.path), info, layer=layer)
        clipped, dropped = clip_features_to_mask(features, self.mask) if self.mask else (features, 0)
        path = self.processed / "layers" / f"{layer}_extract.jsonl"
        from .geom import write_jsonl

        digest = write_jsonl(path, clipped)
        self.record.extract = {
            "used": True,
            **info.as_dict(),
            "clipped": {
                "file": self._rel(path),
                "sha256": digest,
                "features": len(clipped),
                "dropped_outside_boundary": dropped,
            },
        }
        if not info.features_decodable:
            self.record.degradations.append(
                "extract: PBF metadata recorded, features not decoded (see extract.note)"
            )
        return self.record.extract

    def write_manifest(self) -> tuple[Path, str]:
        self.record.finished_at = utc_now()
        self.record.files = self._hash_outputs()
        document = build_manifest(self.config, self.record)
        path = self.processed / MANIFEST_FILE
        digest = write_manifest(
            document, path, config_path=default_config_path(self.config.root)
        )
        write_json(
            self.processed / RUN_RECORD_FILE,
            {
                **_jsonable(asdict(self.record)),
                "manifest_sha256": digest,
                "python": json.dumps({"platform": _platform()}),
            },
        )
        return path, digest

    def close(self) -> None:
        self.client.close()

    # ------------------------------------------------------------------- helpers
    def _count_component(self, source_mode: str) -> None:
        if source_mode == "live":
            self.record.live_components += 1
        else:
            self.record.cached_components += 1

    def _rel(self, path: Path) -> str:
        try:
            return path.relative_to(self.config.root).as_posix()
        except ValueError:
            return path.as_posix()

    def _hash_outputs(self) -> dict[str, Any]:
        files: dict[str, Any] = {}
        for path in sorted(self.processed.rglob("*")):
            if not path.is_file() or path.name in (MANIFEST_FILE, RUN_RECORD_FILE):
                continue
            if path.suffix not in {".json", ".jsonl", ".geojson", ".yaml"}:
                continue
            files[self._rel(path)] = {"sha256": file_sha256(path), "bytes": path.stat().st_size}
        return files


def _sleep(seconds: float) -> None:
    import time

    time.sleep(seconds)


def _platform() -> str:
    import platform

    return f"{platform.system()}-{platform.machine()}-{platform.python_version()}"


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (datetime,)):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def load_layers(config: GisConfig, names: list[str]) -> dict[str, list[dict[str, Any]]]:
    layers: dict[str, list[dict[str, Any]]] = {}
    for name in names:
        path = config.processed_dir / "layers" / f"{name}.jsonl"
        if not path.is_file():
            raise GisEtlError(f"layer file missing: {path}. Run the layers stage first.")
        layers[name] = read_jsonl(path)
    return layers


__all__ = [
    "ChandigarhEtl",
    "CorridorSet",
    "ExtractInfo",
    "inspect_extract",
    "load_config",
]
