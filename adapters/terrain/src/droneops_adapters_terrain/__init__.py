"""terrain: DEM clipping and datum handling adapter.

Reads a *local* raster (GeoTIFF or a numpy array with a georeferencing sidecar), clips
it to a boundary polygon, and writes a record carrying raster version, vertical datum,
resolution, source, retrieval date and clipping method. It does not download anything:
the licensed DEM products this project would use require credentials the repository does
not hold, and a clip whose provenance is "scraped anonymously" is worse than no clip.

Enforced caveat: a 30 m grid cannot represent a tower, a wire or a rooftop, so no clip
produced here is usable for obstacle clearance.

Simulation only. Not authorised for flight.
"""

from __future__ import annotations

from .dem import TERRAIN_CAVEAT, TerrainRecord, clip_dem, load_polygon, read_raster
from .errors import TerrainError, VerticalDatumError
from .geotiff import GeoTiff, read_geotiff, write_geotiff
from .hashing import sha256_of_file

__all__ = [
    "TERRAIN_CAVEAT",
    "TerrainRecord",
    "clip_dem",
    "load_polygon",
    "read_raster",
    "TerrainError",
    "VerticalDatumError",
    "GeoTiff",
    "read_geotiff",
    "write_geotiff",
    "sha256_of_file",
]
