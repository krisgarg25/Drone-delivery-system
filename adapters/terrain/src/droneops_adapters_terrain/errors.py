"""Terrain adapter errors."""

from __future__ import annotations


class TerrainError(Exception):
    """A DEM raster could not be read, georeferenced, clipped or written."""


class VerticalDatumError(TerrainError):
    """The raster's vertical datum is undeclared and the caller wanted clearance.

    Ground elevation without a stated vertical datum is not a height above anything.
    Refusing is cheaper than explaining a 60 m ellipsoid-versus-geoid mistake later.
    """
