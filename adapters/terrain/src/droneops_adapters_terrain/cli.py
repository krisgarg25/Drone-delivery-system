"""``python -m droneops_adapters_terrain.cli`` - inspect and clip a local DEM.

No scraper, no credentials, no download. Copernicus GLO-30 needs an authenticated
account and CC-BY-SA terms; NASADEM and SRTM have their own. The operator who holds
those rights fetches the tile and points this adapter at the path - which is also the
only way the resulting clip can carry a truthful ``source`` and ``retrieval_date``.

    uv run --no-sync python -m droneops_adapters_terrain.cli inspect --raster path/to/tile.tif
    uv run --no-sync python -m droneops_adapters_terrain.cli clip \\
        --raster path/to/tile.tif --polygon data/processed/chandigarh-synthetic-v1/layers/boundary.geojson \\
        --out data/processed/chandigarh-synthetic-v1/terrain \\
        --source "Copernicus GLO-30 (operator-licensed copy)" \\
        --raster-version 202311 --vertical-datum "EGM2008 geoid" --resolution-m 30
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .dem import TERRAIN_CAVEAT, clip_dem, load_polygon, read_raster, summarise
from .errors import TerrainError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="terrain", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    inspect = sub.add_parser("inspect", help="report a raster's grid and georeferencing")
    inspect.add_argument("--raster", type=Path, required=True)

    clip = sub.add_parser("clip", help="crop a raster to a boundary polygon, with provenance")
    clip.add_argument("--raster", type=Path, required=True)
    clip.add_argument("--polygon", type=Path, required=True)
    clip.add_argument("--out", type=Path, required=True)
    clip.add_argument("--source", required=True, help="who published this raster")
    clip.add_argument("--raster-version", required=True, help="edition or product version")
    clip.add_argument(
        "--vertical-datum",
        required=True,
        help='e.g. "EGM2008 geoid", "WGS 84 ellipsoid", or "unknown"',
    )
    clip.add_argument("--resolution-m", type=float, default=30.0)
    clip.add_argument("--retrieval-date", default=None)
    clip.add_argument("--clipping-method", default="polygon-mask-bbox-crop")

    sub.add_parser("caveat", help="print the clearance caveat this adapter enforces")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        payload: dict[str, Any]
        match args.command:
            case "inspect":
                payload = _inspect(args.raster)
            case "clip":
                record = clip_dem(
                    args.raster,
                    args.polygon,
                    args.out,
                    source=args.source,
                    raster_version=args.raster_version,
                    vertical_datum=args.vertical_datum,
                    resolution_m=args.resolution_m,
                    retrieval_date=args.retrieval_date,
                    clipping_method=args.clipping_method,
                )
                record.write(args.out / "terrain_record.yaml")
                summarise([record], args.out / "terrain_summary.yaml")
                payload = record.as_dict()
            case "caveat":
                payload = {"caveat": TERRAIN_CAVEAT, "usable_for_clearance": False}
            case _:  # pragma: no cover
                raise AssertionError(args.command)
    except TerrainError as exc:
        print(f"terrain error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    return 0


def _inspect(path: Path) -> dict[str, Any]:
    raster = read_raster(path)
    return {
        "path": str(path).replace("\\", "/"),
        "width_cells": int(raster.width),
        "height_cells": int(raster.height),
        "pixel_size_degrees": [abs(raster.pixel_size_x), abs(raster.pixel_size_y)],
        "approx_resolution_m": round(abs(raster.pixel_size_x) * 111_320, 1),
        "bbox_deg": list(raster.bbox()),
        "origin": [raster.origin_x, raster.origin_y],
        "nodata": raster.nodata,
        "geo_ascii_params": getattr(raster, "geo_ascii_params", ""),
        "dtype": str(getattr(raster, "data", []).dtype),
        "elevation_min_m": float(raster.data.min()),
        "elevation_max_m": float(raster.data.max()),
        "vertical_datum": "not declared by the raster; must be supplied by the operator",
        "boundary_check": "run clip to intersect with the Chandigarh boundary polygon",
    }


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
