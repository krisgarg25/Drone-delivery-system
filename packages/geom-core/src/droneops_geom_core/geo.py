"""Geodesic distance, bearing and the local planar projection.

Two coordinate systems meet here and the seam is explicit:

* **Geodetic** - WGS 84 longitude/latitude in decimal degrees, floats. This is
  how the world is described and how a corridor centreline is authored.
* **Kernel planar** - integer millimetres east/north of a stated reference in a
  projected frame. This is what a cell index, a volume key and a comparison use.

Floats appear only in the geodetic functions, and every one of them is quantised
to integer millimetres before it can reach a key. That is deliberate: IEEE-754
results are reproducible for a given Python build and architecture, and a run is
identified by ``python_version`` and ``machine_architecture`` (conventions.md
section 4) - but a *stored* resource key has to survive an upgrade of either. So
the float world ends at :meth:`LocalPlane.offset_of`, and
:func:`geodesic_distance_mm` exists so a distance can be compared, sorted and
hashed without ever touching a float.

Projection choice
    An equirectangular tangent plane about a stated reference point: exact in the
    limit at the reference, invertible in closed form, and its error is a
    documented function of distance from the reference rather than a library
    behaviour nobody read. For cell indexing at corridor scale it is far below
    the cell size; anything that must be certified uses a real transverse
    Mercator transform in an adapter, and :data:`MAX_PLANAR_EXTENT_M` enforces
    that this shortcut is not silently stretched across a region.

    The geodesic functions use a spherical earth with the WGS 84 mean radius.
    Haversine on a sphere differs from Vincenty on the ellipsoid by a fraction
    smaller than the synthetic obstacle and corridor errors of this project; the
    bound is stated as :data:`GEODESIC_RELATIVE_TOLERANCE` so a planner and a
    checker can disagree by that much and both still be right.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final

from .altitude import Altitude, AltitudeReference, VerticalDatums
from .cells import Cell2D, Cell3D, GridSpec
from .errors import GeodeticError
from .timebase import MICROS_PER_SECOND, Microsecond, as_microseconds
from .units import Millimetre, as_millimetres, metres_to_millimetres

EARTH_RADIUS_M: Final[float] = 6_371_008.7714
"""WGS 84 mean earth radius (IUGG R1). Spherical model, no flattening term."""

GEODESIC_RELATIVE_TOLERANCE: Final[float] = 5e-3
"""Stated bound on haversine-versus-ellipsoid disagreement for this project."""

LATITUDE_LIMIT_DEG: Final[float] = 90.0
LONGITUDE_LIMIT_DEG: Final[float] = 180.0
POLE_TOLERANCE_DEG: Final[float] = 1e-12
MAX_PLANAR_EXTENT_M: Final[float] = 50_000.0
"""Beyond roughly 50 km from the reference the equirectangular error stops being
negligible next to a corridor margin; ask for a closer local plane."""


def _require_degrees(value: object, name: str, limit: float) -> None:
    """Range-check a degree value taken from an external feed."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GeodeticError(f"{name} must be a number of degrees, got {type(value).__name__}")
    number = float(value)
    if not math.isfinite(number):
        raise GeodeticError(f"{name} is not finite: {number!r}")
    if not -limit <= number <= limit:
        raise GeodeticError(f"{name} {number} deg is outside +-{limit}")


@dataclass(frozen=True)
class GeoPoint:
    """A geodetic position in decimal degrees, range-checked on construction."""

    lat_deg: float
    lon_deg: float

    def __post_init__(self) -> None:
        _require_degrees(self.lat_deg, "latitude", LATITUDE_LIMIT_DEG)
        _require_degrees(self.lon_deg, "longitude", LONGITUDE_LIMIT_DEG)

    @property
    def lat_rad(self) -> float:
        return math.radians(self.lat_deg)

    @property
    def lon_rad(self) -> float:
        return math.radians(self.lon_deg)

    def is_pole(self) -> bool:
        """True at a pole, where east is undefined and a bearing degenerates."""
        return abs(abs(self.lat_deg) - LATITUDE_LIMIT_DEG) < POLE_TOLERANCE_DEG

    def sort_key(self) -> tuple[float, float]:
        """Ascending ``(lat, lon)``; only for enumeration, never for proximity."""
        return (self.lat_deg, self.lon_deg)


@dataclass(frozen=True)
class PlanarOffset:
    """Integer millimetres east and north of a local plane's reference point."""

    east_mm: Millimetre
    north_mm: Millimetre

    @property
    def as_pair(self) -> tuple[Millimetre, Millimetre]:
        """``(east_mm, north_mm)`` for unpacking into a grid call."""
        return (self.east_mm, self.north_mm)

    @property
    def measure_mm(self) -> Millimetre:
        """Integer floor of the planar distance from the reference.

        ``math.isqrt`` keeps this exact and float-free, so it is safe to use for
        a conservative pre-filter radius. It is a *planar* distance: do not
        confuse it with a geodesic one.
        """
        return Millimetre(math.isqrt(self.east_mm * self.east_mm + self.north_mm * self.north_mm))


@dataclass(frozen=True)
class LocalPlane:
    """A tangent plane anchored at a geodetic reference point."""

    reference: GeoPoint

    def __post_init__(self) -> None:
        if self.reference.is_pole():
            raise GeodeticError("a local plane cannot be anchored at a pole: east is undefined")

    @property
    def cos_reference(self) -> float:
        return math.cos(self.reference.lat_rad)

    def offset_of(self, point: GeoPoint) -> PlanarOffset:
        """Geodetic to planar, quantised once to integer millimetres.

        Raises:
            GeodeticError: the point is further than :data:`MAX_PLANAR_EXTENT_M`
                from the reference. Refusing beats accumulating error.
        """
        east_m = EARTH_RADIUS_M * (point.lon_rad - self.reference.lon_rad) * self.cos_reference
        north_m = EARTH_RADIUS_M * (point.lat_rad - self.reference.lat_rad)
        if abs(east_m) > MAX_PLANAR_EXTENT_M or abs(north_m) > MAX_PLANAR_EXTENT_M:
            raise GeodeticError(
                f"offset ({east_m:.1f}, {north_m:.1f}) m exceeds the documented "
                f"{MAX_PLANAR_EXTENT_M:.0f} m extent of this projection; choose a closer reference"
            )
        return PlanarOffset(metres_to_millimetres(east_m), metres_to_millimetres(north_m))

    def point_at(self, offset: PlanarOffset) -> GeoPoint:
        """Planar to geodetic: the closed-form inverse, up to quantisation."""
        lat_rad = self.reference.lat_rad + offset.north_mm / 1_000.0 / EARTH_RADIUS_M
        lon_rad = self.reference.lon_rad + offset.east_mm / 1_000.0 / EARTH_RADIUS_M / (
            self.cos_reference
        )
        lat_deg = math.degrees(lat_rad)
        if not -LATITUDE_LIMIT_DEG <= lat_deg <= LATITUDE_LIMIT_DEG:
            raise GeodeticError(f"projected latitude {lat_deg} deg leaves the globe")
        return GeoPoint(lat_deg, wrap_longitude(math.degrees(lon_rad)))

    def cell_of(self, grid: GridSpec, point: GeoPoint) -> Cell2D:
        """The fine grid cell containing a geodetic point."""
        offset = self.offset_of(point)
        return grid.cell_of(offset.east_mm, offset.north_mm)

    def volume_cell_of(
        self,
        grid: GridSpec,
        point: GeoPoint,
        altitude_mm: int,
        *,
        level: int,
        reference: AltitudeReference,
    ) -> Cell3D:
        """The 3D volume cell containing a geodetic point and an altitude.

        ``reference`` is required, with no default: a volume key whose vertical
        frame is unknown is not a key but a defect waiting to be reserved
        (invariant I9).
        """
        offset = self.offset_of(point)
        return grid.volume_cell_of(
            offset.east_mm,
            offset.north_mm,
            as_millimetres(altitude_mm),
            level=level,
            reference=reference,
        )


@dataclass(frozen=True)
class Waypoint:
    """A position, an altitude and the kernel time it was true.

    This is the smallest record that can satisfy invariant I9: the altitude is a
    :class:`~droneops_geom_core.altitude.Altitude`, so it cannot exist without a
    reference, and a plan point or telemetry sample built from a bare float
    altitude cannot be expressed at all.
    """

    point: GeoPoint
    altitude: Altitude
    at_us: Microsecond

    def __post_init__(self) -> None:
        as_microseconds(self.at_us)

    def altitude_in(
        self, reference: AltitudeReference, datums: VerticalDatums
    ) -> Waypoint:
        """The same waypoint with its altitude converted. Keeps the position."""
        return Waypoint(
            self.point, self.altitude.as_reference(reference, datums), self.at_us
        )

    def sort_key(self) -> tuple[int, float, float]:
        """Ascending kernel time, then position - the enumeration order."""
        return (self.at_us, self.point.lat_deg, self.point.lon_deg)


def haversine_distance_m(left: GeoPoint, right: GeoPoint) -> float:
    """Great-circle distance in metres on the spherical model.

    Symmetric by construction: every intermediate term is even in the sign of the
    coordinate differences, so ``d(a, b) == d(b, a)`` bit for bit.
    """
    delta_lat = right.lat_rad - left.lat_rad
    delta_lon = right.lon_rad - left.lon_rad
    sine_lat = math.sin(delta_lat / 2.0)
    sine_lon = math.sin(delta_lon / 2.0)
    cosine_product = math.cos(left.lat_rad) * math.cos(right.lat_rad)
    swept = sine_lat * sine_lat + cosine_product * sine_lon * sine_lon
    return 2.0 * EARTH_RADIUS_M * math.asin(math.sqrt(min(1.0, swept)))


def geodesic_distance_mm(left: GeoPoint, right: GeoPoint) -> Millimetre:
    """The same distance, quantised to integer millimetres.

    Use this whenever a distance is compared, sorted, thresholded or stored.
    """
    return metres_to_millimetres(haversine_distance_m(left, right))


def initial_bearing_deg(left: GeoPoint, right: GeoPoint) -> float:
    """Forward azimuth in degrees, ``[0, 360)``, true north at zero.

    Raises:
        GeodeticError: the points coincide, or either is a pole. A bearing
            between identical points is undefined, and returning ``0.0`` would
            report "heading north" where nothing was measured.
    """
    if left == right:
        raise GeodeticError("bearing between identical points is undefined")
    if left.is_pole() or right.is_pole():
        raise GeodeticError("bearing to or from a pole is undefined")
    delta_lon = right.lon_rad - left.lon_rad
    y = math.sin(delta_lon) * math.cos(right.lat_rad)
    x = math.cos(left.lat_rad) * math.sin(right.lat_rad) - math.sin(left.lat_rad) * math.cos(
        right.lat_rad
    ) * math.cos(delta_lon)
    return normalize_deg(math.degrees(math.atan2(y, x)))


def final_bearing_deg(left: GeoPoint, right: GeoPoint) -> float:
    """Arrival azimuth, so a corridor's bend can be measured.

    ``final(A -> B)`` is the reverse initial bearing rotated by 180 degrees,
    which holds on a sphere exactly.
    """
    return normalize_deg(initial_bearing_deg(right, left) + 180.0)


def destination_point(origin: GeoPoint, bearing_deg: float, distance_m: float) -> GeoPoint:
    """The geodetic point ``distance_m`` ahead on a great circle."""
    if distance_m < 0.0:
        raise GeodeticError(f"distance must be non-negative, got {distance_m}")
    if origin.is_pole():
        raise GeodeticError("a great-circle path from a pole has no unique initial bearing")
    angular = distance_m / EARTH_RADIUS_M
    bearing = math.radians(normalize_deg(bearing_deg))
    sine_lat = math.sin(origin.lat_rad)
    cosine_lat = math.cos(origin.lat_rad)
    sine_angular = math.sin(angular)
    cosine_angular = math.cos(angular)
    lat = math.asin(sine_lat * cosine_angular + cosine_lat * sine_angular * math.cos(bearing))
    lon = origin.lon_rad + math.atan2(
        math.sin(bearing) * sine_angular * cosine_lat,
        cosine_angular - sine_lat * math.sin(lat),
    )
    return GeoPoint(math.degrees(lat), wrap_longitude(math.degrees(lon)))


def wrap_longitude(lon_deg: float) -> float:
    """Fold a longitude into ``(-180, 180]``. The anti-meridian is a seam, not an edge."""
    wrapped = math.fmod(lon_deg + 180.0, 360.0)
    if wrapped <= 0.0:
        wrapped += 360.0
    return wrapped - 180.0


def normalize_deg(angle_deg: float) -> float:
    """Fold an angle into ``[0, 360)``."""
    value = math.fmod(angle_deg, 360.0)
    return value + 360.0 if value < 0.0 else value


def traversal_microseconds(distance_m: float, speed_mm_per_s: int) -> Microsecond:
    """Time to cover a geodesic distance at a constant ground speed.

    ``speed_mm_per_s`` is integer millimetres per second, so the division is
    exact integer arithmetic. The result is the ceiling: an estimate must never
    be shorter than the truth, because a traversal rounded down is a reservation
    that ends while an aircraft is still inside the volume.

    Raises:
        GeodeticError: negative distance, or a non-positive speed.
    """
    if distance_m < 0.0:
        raise GeodeticError(f"distance must be non-negative, got {distance_m}")
    if speed_mm_per_s <= 0:
        raise GeodeticError(f"speed must be positive millimetres per second, got {speed_mm_per_s}")
    distance_mm = int(metres_to_millimetres(distance_m))
    numerator = distance_mm * MICROS_PER_SECOND
    return Microsecond(-((-numerator) // speed_mm_per_s))
