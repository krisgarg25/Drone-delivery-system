"""``python -m droneops_adapters_gis_etl.cli`` - the operator's entry point.

Every stage is separately runnable and separately re-runnable from the cache, because
the real constraint on this workstream is a public Overpass endpoint that answers 504
without warning. A stage that fails loses its own chunk, not the run.

    uv run --no-sync python -m droneops_adapters_gis_etl.cli run-all
    uv run --no-sync python -m droneops_adapters_gis_etl.cli run-all --offline
    uv run --no-sync python -m droneops_adapters_gis_etl.cli audit
    uv run --no-sync python -m droneops_adapters_gis_etl.cli inspect-extract --extract <path>

There is no command here that plans, reserves or dispatches: an adapter writes
snapshots and the core evaluates them (conventions section 7).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .config import GisConfig, load_config
from .coverage import load_report
from .errors import GisEtlError
from .geom import read_jsonl
from .manifest import load_manifest
from .pipeline import ChandigarhEtl
from .sources import inspect_extract


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gis-etl", description=__doc__.splitlines()[0])
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="versioned config (default: configs/gis/chandigarh-synthetic-v1.yaml)",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="never touch the network; replay from data/raw and say so in the manifest",
    )
    parser.add_argument("--quiet", action="store_true", help="machine-readable output only")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("probe", help="measure coverage metrics only (cheap, ~12 count queries)")
    sub.add_parser("boundary", help="retrieve and verify the administrative boundary")
    layers = sub.add_parser("layers", help="retrieve and clip the feature layers")
    layers.add_argument("--only", nargs="*", default=None, help="subset of layer names")
    sub.add_parser("obstacles", help="generate the synthetic vertical model from cached layers")
    sub.add_parser("corridors", help="derive candidate corridors (heuristic only)")
    sub.add_parser("manifest", help="write manifest.yaml from the artefacts on disk")
    run_all = sub.add_parser("run-all", help="every stage, in order")
    run_all.add_argument("--extract", type=Path, default=None, help="local bulk extract path")
    run_all.add_argument("--layers", nargs="*", default=None)
    inspect = sub.add_parser("inspect-extract", help="record an extract's identity")
    inspect.add_argument("--extract", type=Path, required=True)
    inspect.add_argument("--source", default="", help="source URL or provenance note")
    ingest = sub.add_parser("ingest-extract", help="ingest + clip a supported local extract")
    ingest.add_argument("--extract", type=Path, required=True)
    ingest.add_argument("--source", default="")
    ingest.add_argument("--layer", default="extract")
    sub.add_parser("audit", help="lint every generated obstacle and corridor on disk")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
    except GisEtlError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    live = not args.offline
    payload: Any
    match args.command:
        case "probe":
            payload = _probe(config, live)
        case "boundary":
            payload = _boundary(config, live)
        case "layers":
            payload = _layers(config, live, args.only)
        case "obstacles":
            payload = _obstacles(config)
        case "corridors":
            payload = _corridors(config)
        case "manifest":
            payload = _manifest(config)
        case "run-all":
            payload = _run_all(config, live, args)
        case "inspect-extract":
            payload = inspect_extract(args.extract, source=args.source).as_dict()
        case "ingest-extract":
            payload = _ingest(config, live, args)
        case "audit":
            payload = _audit(config)
        case _:  # pragma: no cover - argparse enforces the choices
            raise AssertionError(args.command)
    if args.quiet:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    else:
        _report_human(args.command, payload)
    return 0


# ------------------------------------------------------------------------ stages


def _etl(config: GisConfig, live: bool) -> ChandigarhEtl:
    return ChandigarhEtl(config, live=live)


def _probe(config: GisConfig, live: bool) -> dict[str, Any]:
    etl = _etl(config, live)
    try:
        report = etl.run_coverage()
    finally:
        etl.close()
    return {
        "measured_at": report.get("measured_at"),
        "claim_current": report.get("claim_current"),
        "live_metrics": report.get("live_metrics"),
        "cached_metrics": report.get("cached_metrics"),
        "coverage": report.get("coverage"),
        "failures": report.get("failures"),
        "verdict": report.get("verdict"),
        "file": report.get("file"),
    }


def _boundary(config: GisConfig, live: bool) -> dict[str, Any]:
    etl = _etl(config, live)
    try:
        return etl.run_boundary()
    finally:
        etl.close()


def _layers(config: GisConfig, live: bool, only: list[str] | None) -> dict[str, Any]:
    etl = _etl(config, live)
    try:
        etl.run_boundary()
        outcomes = etl.run_layers(only)
        etl.record.files = etl._hash_outputs()
        return {
            name: {
                "features": outcome.get("features") if isinstance(outcome, dict) else None,
                "chunks": outcome.get("chunks") if isinstance(outcome, dict) else None,
                "degraded": outcome.get("degraded") if isinstance(outcome, dict) else None,
                "files": outcome.get("files") if isinstance(outcome, dict) else None,
            }
            for name, outcome in outcomes.items()
        }
    finally:
        etl.close()


def _obstacles(config: GisConfig) -> dict[str, Any]:
    etl = _etl(config, live=False)
    try:
        etl._layers = {
            name: read_jsonl(config.processed_dir / "layers" / f"{name}.jsonl")
            for name in ("buildings", "power_lines", "towers", "trees")
            if (config.processed_dir / "layers" / f"{name}.jsonl").is_file()
        }
        return etl.run_obstacles()
    finally:
        etl.close()


def _corridors(config: GisConfig) -> dict[str, Any]:
    etl = _etl(config, live=False)
    try:
        etl._layers = {
            name: read_jsonl(config.processed_dir / "layers" / f"{name}.jsonl")
            for name in ("roads", "parks", "buildings")
            if (config.processed_dir / "layers" / f"{name}.jsonl").is_file()
        }
        return etl.run_corridors()
    finally:
        etl.close()


def _manifest(config: GisConfig) -> dict[str, Any]:
    """Rebuild a manifest purely from artefacts already on disk."""
    etl = _etl(config, live=False)
    try:
        return _manifest_from_disk(config, etl)
    finally:
        etl.close()


def _manifest_from_disk(config: GisConfig, etl: ChandigarhEtl) -> dict[str, Any]:
    from .pipeline import COVERAGE_FILE, MANIFEST_FILE

    processed = config.processed_dir
    boundary_path = processed / "layers" / "boundary.geojson"
    if not boundary_path.is_file():
        raise GisEtlError(f"cannot write a manifest without {boundary_path}")
    collection = json.loads(boundary_path.read_text(encoding="utf-8"))
    feature = collection["features"][0]
    properties = feature["properties"]
    provenance = properties.get("provenance", {})
    from .hashing import file_sha256

    etl.record.boundary = {
        "hash": file_sha256(boundary_path),
        "file": "data boundary geojson",
        "name": properties.get("name", ""),
        "admin_level": properties.get("admin_level", ""),
        "iso3166_2": properties.get("iso3166_2", ""),
        "wikidata": properties.get("wikidata", ""),
        "licence": provenance.get("licence", ""),
        "attribution": provenance.get("attribution", ""),
        "retrieved_at": provenance.get("retrieved_at", ""),
        "endpoint": provenance.get("endpoint", ""),
        "query_sha256": provenance.get("query_sha256", ""),
    }
    etl.record.coverage = load_report(processed / COVERAGE_FILE)
    obstacles_summary = processed / "obstacles" / "obstacles_summary.json"
    if obstacles_summary.is_file():
        etl.record.obstacles = {
            **json.loads(obstacles_summary.read_text(encoding="utf-8")),
            "hash": file_sha256(processed / "obstacles" / "obstacles.jsonl"),
        }
    corridors_summary = processed / "corridors" / "corridors_summary.json"
    if corridors_summary.is_file():
        corridor_geojson = processed / "corridors" / "candidate_corridors.geojson"
        etl.record.corridors = {
            **json.loads(corridors_summary.read_text(encoding="utf-8")),
            "hash": file_sha256(corridor_geojson) if corridor_geojson.is_file() else "",
        }
    for path in sorted((processed / "layers").glob("*.jsonl")):
        etl.record.layers[path.stem] = {"files": {path.name: {"sha256": file_sha256(path)}}}
    path, digest = etl.write_manifest()
    return {"manifest": path.as_posix(), "sha256": digest, "keys": sorted(load_manifest(path))}


def _run_all(config: GisConfig, live: bool, args: argparse.Namespace) -> dict[str, Any]:
    etl = _etl(config, live)
    try:
        etl.run_boundary()
        etl.run_layers(args.layers)
        etl.run_obstacles()
        etl.run_corridors()
        etl.run_coverage()
        if args.extract:
            info = inspect_extract(args.extract)
            etl.run_extract(info, layer=getattr(args, "layer", None) or "extract")
        path, digest = etl.write_manifest()
        return {
            "manifest": path.as_posix(),
            "manifest_sha256": digest,
            "live_components": etl.record.live_components,
            "cached_components": etl.record.cached_components,
            "failed_components": etl.record.failed_components,
            "degradations": etl.record.degradations,
            "obstacles": etl.record.obstacles.get("by_kind"),
            "corridors": etl.record.corridors.get("corridors"),
            "coverage": etl.record.coverage.get("coverage"),
            "boundary": etl.record.boundary.get("name"),
        }
    finally:
        etl.close()


def _ingest(config: GisConfig, live: bool, args: argparse.Namespace) -> dict[str, Any]:
    etl = _etl(config, live)
    try:
        etl.run_boundary()
        info = inspect_extract(args.extract, source=args.source)
        return etl.run_extract(info, layer=args.layer)
    finally:
        etl.close()


def _audit(config: GisConfig) -> dict[str, Any]:
    """Independent re-read of the artefacts: the flags must survive the write."""
    from .audit import audit_snapshot

    return audit_snapshot(config)


# ------------------------------------------------------------------------- output


def _report_human(command: str, payload: Any) -> None:
    print(f"[gis-etl] {command}")
    if isinstance(payload, dict):
        for key, value in payload.items():
            if isinstance(value, dict) and len(value) > 3:
                print(f"  {key}:")
                for inner, inner_value in list(value.items())[:12]:
                    print(f"    {inner}: {_brief(inner_value)}")
            else:
                print(f"  {key}: {_brief(value)}")
    else:  # pragma: no cover - every command returns a mapping
        print(json.dumps(payload, indent=2, default=str))


def _brief(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return f"[{len(value)} items] " + ", ".join(_brief(item) for item in list(value)[:3])
    if isinstance(value, dict):
        return json.dumps(value, default=str)[:160]
    text = str(value)
    return text if len(text) <= 160 else text[:157] + "..."


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
