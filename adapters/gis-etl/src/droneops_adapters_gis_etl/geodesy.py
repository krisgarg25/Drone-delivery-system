"""Coarse geodesy for grouping, not for reservation keys.

The authority for anything a kernel consumes is ``packages/geom-core`` (pure stdlib,
integer-millimetre quantisation, documented projection error). This adapter does not
declare that dependency and does not import it: a Phase 1 adapter must be installable
on its own, and these numbers only ever decide *which road segments are drawn as one
corridor* - a visual grouping, never a distance a reservation is compared against.

The formulas are the same ones geom-core documents: a spherical earth with the WGS 84
mean radius, and an equirectangular tangent plane about the configured reference
latitude. Error budget: haversine versus the ellipsoid is under
:data:`GEODESIC_RELATIVE_TOLERANCE`, far below the synthetic obstacle uncertainty this
dataset carries, and the grouping tolerance is two orders of magnitude coarser again.
"""

from __future__ import annotations

import math
from typing import Final

EARTH_RADIUS_M: Final[float] = 6_371_008.7714
"""WGS 84 mean earth radius, matching ``droneops_geom_core.geo``."""

GEODESIC_RELATIVE_TOLERANCE: Final[float] = 5e-3

Point = tuple[float, float]


def haversine_m(a: Point, b: Point) -> float:
    """Great-circle distance in metres between ``(lat, lon)`` pairs."""
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    swept = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2.0 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(swept)))


def bearing_deg(a: Point, b: Point) -> float:
    """Forward azimuth in degrees, [0, 360). Undefined for coincident points -> 0.0."""
    if a == b:
        return 0.0
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlon = lon2 - lon1
    y = math.sin(dlon) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def length_m(coords: list[list[float]]) -> float:
    """Total length of a ``(lon, lat)`` polyline, summed geodesically."""
    total = 0.0
    previous: Point | None = None
    for coordinate in coords:
        point = (float(coordinate[1]), float(coordinate[0]))
        if previous is not None:
            total += haversine_m(previous, point)
        previous = point
    return total


def to_plane(point: Point, reference_lat_deg: float) -> tuple[float, float]:
    """Equirectangular metres ``(east, north)`` about the reference latitude."""
    circumference_lat = 2 * math.pi * EARTH_RADIUS_M
    metres_per_deg_lat = circumference_lat / 360.0
    metres_per_deg_lon = metres_per_deg_lat * math.cos(math.radians(reference_lat_deg))
    return (point[1] * metres_per_deg_lon, point[0] * metres_per_deg_lat)


def orientation_bucket(bearing: float, bucket_deg: float = 15.0) -> int:
    """An *undirected* heading bucket: 10 deg and 190 deg are the same corridor axis."""
    normalised = bearing % 180.0
    return int(round(normalised / bucket_deg) % int(180.0 // bucket_deg))
