"""Terrain: a local raster in, a provenance-complete clip out - and no network.

These tests are the contract for ``adapters/terrain`` (spec 12 section 5): the record
must carry raster version, vertical datum, resolution, source, retrieval date and
clipping method, and the 30 m caveat must survive every path through the module. The
last test is structural: this adapter must never grow a DEM scraper, because the
products it would clip are licensed and a clip whose provenance is "scraped anonymously"
is worse than no clip at all.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import yaml
from droneops_adapters_terrain.cli import main
from droneops_adapters_terrain.dem import (
    TERRAIN_CAVEAT,
    clearance_guard,
    clip_dem,
    read_raster,
)
from droneops_adapters_terrain.errors import TerrainError, VerticalDatumError
from droneops_adapters_terrain.geotiff import read_geotiff, write_geotiff

PIXEL = 30.0 / 111_320.0  # ~30 m expressed in degrees at the equator


def make_raster(directory: Path, *, width: int = 40, height: int = 30) -> Path:
    """A 30 m grid with a tilted ground surface, as .npz plus a georeferencing sidecar."""
    data = 260.0 + np.arange(width)[None, :] * 0.5 + np.arange(height)[:, None] * 0.25
    path = directory / "tile.npz"
    np.savez_compressed(path, data=data.astype(np.float32))
    (directory / "tile.yaml").write_text(
        yaml.safe_dump(
            {
                "origin_x": 76.72,
                "origin_y": 30.78,
                "pixel_size_x": PIXEL,
                "pixel_size_y": -PIXEL,
                "nodata": -32768.0,
                "geo_ascii_params": 'GEOGCS["WGS 84",DATUM["WGS_1984"]]',
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


def make_polygon(directory: Path) -> Path:
    """A diamond well inside the test tile, so the crop has cells on both sides of it."""
    ring = [
        [76.7220, 30.7755],
        [76.7290, 30.7780],
        [76.7290, 30.7730],
        [76.7220, 30.7755],
    ]
    payload = {
        "type": "FeatureCollection",
        "authoritative": False,
        "features": [
            {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [ring]}, "properties": {}}
        ],
    }
    path = directory / "boundary.geojson"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@pytest.fixture
def clip(tmp_path: Path) -> Any:
    raster = make_raster(tmp_path)
    polygon = make_polygon(tmp_path)
    record = clip_dem(
        raster,
        polygon,
        tmp_path / "out",
        source="Copernicus GLO-30 (operator-licensed copy)",
        raster_version="202311",
        vertical_datum="EGM2008 geoid",
        resolution_m=30.0,
        retrieval_date="2026-09-30T00:00:00+00:00",
    )
    return record, tmp_path


def test_clip_record_carries_every_field_the_manifest_needs(clip: Any) -> None:
    record, tmp_path = clip
    for field in (
        "source",
        "raster_version",
        "vertical_datum",
        "resolution_m",
        "retrieval_date",
        "clipping_method",
    ):
        assert getattr(record, field), f"{field} is empty"
    assert record.raster_version == "202311"
    assert record.vertical_datum == "EGM2008 geoid"
    assert record.datum_declared is True
    assert record.resolution_m == 30.0
    assert record.clipping_method == "polygon-mask-bbox-crop"
    assert record.retrieval_date == "2026-09-30T00:00:00+00:00"
    assert record.raster_sha256.startswith("sha256:") or len(record.raster_sha256) == 64
    assert (tmp_path / "out" / "dem_clip.tif").is_file()
    assert record.valid_cells > 0 and record.masked_cells > 0
    assert record.elevation_min_m is not None and record.elevation_max_m > record.elevation_min_m


def test_the_written_clip_is_a_readable_geotiff(clip: Any) -> None:
    """The clip is an artefact other code will open, so it is read back, not trusted."""
    record, tmp_path = clip
    raster = read_geotiff(tmp_path / "out" / "dem_clip.tif")
    assert raster.width == record.width_cells
    assert raster.height == record.height_cells
    assert float(np.nanmax(raster.data)) == pytest.approx(record.elevation_max_m, abs=1e-3)


def test_no_clip_is_usable_for_clearance_and_the_guard_proves_it(clip: Any) -> None:
    record, _ = clip
    assert record.usable_for_clearance is False
    clearance_guard(record)  # an honest clip is refused nothing it did not claim

    # The guard exists so the refusal is a checked invariant, not a comment: a record
    # promoted by hand into "usable for clearance" is rejected outright.
    record.usable_for_clearance = True
    with pytest.raises(VerticalDatumError, match="usable for clearance"):
        clearance_guard(record)


def test_an_undeclared_datum_is_refused_by_the_guard(tmp_path: Path) -> None:
    record = clip_dem(
        make_raster(tmp_path),
        make_polygon(tmp_path),
        tmp_path / "out2",
        source="SRTM v3 (operator copy)",
        raster_version="v3.0",
        vertical_datum="unknown",
        resolution_m=30.0,
    )
    with pytest.raises(VerticalDatumError, match="vertical datum"):
        clearance_guard(record)


def test_the_caveat_names_what_a_30_m_grid_cannot_see(clip: Any) -> None:
    record, _ = clip
    lowered = TERRAIN_CAVEAT.lower()
    for word in ("30 m", "tower", "wire", "rooftop"):
        assert word in lowered, TERRAIN_CAVEAT
    assert "30 m" in record.caveat.lower()


def test_undeclared_datum_is_recorded_as_unknown_and_flagged(tmp_path: Path) -> None:
    record = clip_dem(
        make_raster(tmp_path),
        make_polygon(tmp_path),
        tmp_path / "out",
        source="SRTM v3 (operator copy)",
        raster_version="v3.0",
        vertical_datum="unknown",
        resolution_m=30.0,
    )
    assert record.datum_declared is False
    assert record.vertical_datum == "unknown"
    assert "datum" in record.extra["datum_action_required"].lower()


def test_a_polygon_that_misses_the_raster_raises_instead_of_writing_nothing(
    tmp_path: Path
) -> None:
    """An empty clip would be published as "terrain for Chandigarh"."""
    ring = [[77.9, 31.9], [77.95, 31.9], [77.95, 31.95], [77.9, 31.95], [77.9, 31.9]]
    path = tmp_path / "far.geojson"
    path.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [ring]}, "properties": {}}
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(TerrainError, match="does not touch the raster"):
        clip_dem(
            make_raster(tmp_path),
            path,
            tmp_path / "out",
            source="x",
            raster_version="1",
            vertical_datum="EGM2008 geoid",
            resolution_m=30.0,
        )
    assert not (tmp_path / "out" / "dem_clip.tif").exists()


def test_read_raster_refuses_an_unsupported_or_unanchored_input(tmp_path: Path) -> None:
    (tmp_path / "tile.img").write_bytes(b"\x00\x01")
    with pytest.raises(TerrainError):
        read_raster(tmp_path / "tile.img")
    data = np.zeros((4, 4), dtype=np.float32)
    np.save(tmp_path / "bare.npy", data)
    with pytest.raises(TerrainError, match="sidecar"):
        read_raster(tmp_path / "bare.npy")


def test_cli_clip_writes_record_and_summary(tmp_path: Path) -> None:
    raster = make_raster(tmp_path)
    polygon = make_polygon(tmp_path)
    out = tmp_path / "terrain"
    code = main(
        [
            "clip",
            "--raster",
            str(raster),
            "--polygon",
            str(polygon),
            "--out",
            str(out),
            "--source",
            "Copernicus GLO-30 (operator-licensed copy)",
            "--raster-version",
            "202311",
            "--vertical-datum",
            "EGM2008 geoid",
            "--resolution-m",
            "30",
        ]
    )
    assert code == 0
    record = yaml.safe_load((out / "terrain_record.yaml").read_text(encoding="utf-8"))
    summary = yaml.safe_load((out / "terrain_summary.yaml").read_text(encoding="utf-8"))
    assert record["vertical_datum"] == "EGM2008 geoid"
    assert record["usable_for_clearance"] is False
    assert summary["authoritative"] is False
    assert "not authorised for flight" in summary["statement"].lower()


def test_geotiff_round_trip_is_lossless(tmp_path: Path) -> None:
    data = (280 + np.arange(12)[None, :] + np.arange(9)[:, None] * 2).astype(np.float32)
    path = tmp_path / "rt.tif"
    write_geotiff(
        path,
        data,
        origin_x=76.72,
        origin_y=30.78,
        pixel_size_x=PIXEL,
        pixel_size_y=-PIXEL,
        nodata=-9999.0,
    )
    back = read_geotiff(path)
    assert np.array_equal(back.data, data)
    assert back.width == 12 and back.height == 9


def test_this_adapter_has_no_downloader_and_no_credentials() -> None:
    """Structural: a scraper for a licensed DEM must never appear here.

    Copernicus and NASADEM are account-licensed products, so the only honest source of a
    raster is a path the operator already has rights to. This test fails the moment
    someone adds an HTTP client or a token to the terrain adapter.
    """
    src = Path(__file__).resolve().parents[1] / "src" / "droneops_adapters_terrain"
    banned = ("httpx", "requests", "urllib.request", "socket", "urlretrieve", "api_key", "token")
    for path in sorted(src.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for needle in banned:
            assert needle not in text, f"{path.name} references {needle!r}"
