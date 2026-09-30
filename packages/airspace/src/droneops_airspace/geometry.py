"""Planar footprints for restrictions: exact integer predicates, no float epsilon.

Coordinates arrive geodetic and are projected once onto a :class:`LocalPlane`
anchored at the footprint's first vertex, then handled in integer millimetres
(``geom-core`` rule: floats survive only inside geodesic formulas). A footprint
answers two questions and nothing else:

* ``contains(point)`` - is this place inside the restricted footprint;
* ``crossed_segments(path)`` - which consecutive pairs of a path enter it.

Four decisions worth stating:

1. **Touching counts as intersecting.** A route that grazes a boundary vertex is
   reported as intersecting. Under-reporting an overlap would be a false "allowed",
   so every boundary case errs toward the restriction.
2. **A buffered line is decided by an exact segment-to-segment distance test.**
   For two non-crossing planar segments the minimum distance is attained at an
   endpoint of one of them, so four point-to-segment tests are complete, not an
   approximation. Distances are compared as squared integers: no epsilon, no
   rounding, the same answer on every machine (invariant I7).
3. **The pre-filter is wide and only ever skips a point.** ``reach_mm`` covers the
   whole footprint plus 100 m of slack, so a point beyond it cannot be inside.
4. **An unprojectable query is an error, never a "no".** A path whose points do
   not fit the footprint's tangent plane raises: answering "no intersection"
   without having looked would be exactly the false clear this package forbids.
"""

from __future__ import annotations

import math
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum

from droneops_geom_core import (
    GeoPoint,
    LocalPlane,
    Millimetre,
    PlanarOffset,
    geodesic_distance_mm,
)

from .errors import GeometryInputError

PRE_FILTER_MARGIN_MM: Millimetre = Millimetre(100_000)
"""100 m of slack on the geodesic reach bound: generous, cheap, never decisive."""

MIN_CLOSED_RING_POINTS = 4
MIN_LINE_POINTS = 2


class FootprintShape(StrEnum):
    POLYGON = "polygon"
    CIRCLE = "circle"
    BUFFERED_LINE = "buffered_line"


def _e(offset: PlanarOffset) -> int:
    return int(offset.east_mm)


def _n(offset: PlanarOffset) -> int:
    return int(offset.north_mm)


def _orient(a: PlanarOffset, b: PlanarOffset, c: PlanarOffset) -> int:
    """Sign of the cross product AB x AC, in integer millimetres."""

    value = (_e(b) - _e(a)) * (_n(c) - _n(a)) - (_n(b) - _n(a)) * (_e(c) - _e(a))
    return (value > 0) - (value < 0)


def _squared(a: PlanarOffset, b: PlanarOffset) -> int:
    de = _e(a) - _e(b)
    dn = _n(a) - _n(b)
    return de * de + dn * dn


def _between(value: int, low: int, high: int) -> bool:
    return min(low, high) <= value <= max(low, high)


def _on_segment(p: PlanarOffset, a: PlanarOffset, b: PlanarOffset) -> bool:
    """Bounding-box test for a point already known to be collinear with AB."""

    return _between(_e(p), _e(a), _e(b)) and _between(_n(p), _n(a), _n(b))


def _segments_meet(a: PlanarOffset, b: PlanarOffset, c: PlanarOffset, d: PlanarOffset) -> bool:
    """True when segments AB and CD cross, touch or overlap."""

    o1 = _orient(a, b, c)
    o2 = _orient(a, b, d)
    o3 = _orient(c, d, a)
    o4 = _orient(c, d, b)
    if o1 == 0 and _on_segment(c, a, b):
        return True
    if o2 == 0 and _on_segment(d, a, b):
        return True
    if o3 == 0 and _on_segment(a, c, d):
        return True
    if o4 == 0 and _on_segment(b, c, d):
        return True
    return (o1 > 0) != (o2 > 0) and (o3 > 0) != (o4 > 0)


def _within_distance(p: PlanarOffset, a: PlanarOffset, b: PlanarOffset, reach_mm: int) -> bool:
    """Exact integer test: is ``p`` within ``reach_mm`` of segment AB?

    Squared comparisons only, so a near-miss is decided by arithmetic rather than
    by an epsilon that could differ between runs.
    """

    limit2 = reach_mm * reach_mm
    if _squared(p, a) <= limit2 or _squared(p, b) <= limit2:
        return True
    de = _e(b) - _e(a)
    dn = _n(b) - _n(a)
    length2 = de * de + dn * dn
    if length2 == 0:
        return False
    dot = (_e(p) - _e(a)) * de + (_n(p) - _n(a)) * dn
    if dot <= 0 or dot >= length2:
        return False
    cross = de * (_n(p) - _n(a)) - dn * (_e(p) - _e(a))
    return cross * cross <= limit2 * length2


def _segments_within(
    a: PlanarOffset,
    b: PlanarOffset,
    c: PlanarOffset,
    d: PlanarOffset,
    reach_mm: int,
) -> bool:
    """Minimum distance between two segments <= ``reach_mm``, decided exactly."""

    if _segments_meet(a, b, c, d):
        return True
    return (
        _within_distance(a, c, d, reach_mm)
        or _within_distance(b, c, d, reach_mm)
        or _within_distance(c, a, b, reach_mm)
        or _within_distance(d, a, b, reach_mm)
    )


def _point_in_ring(p: PlanarOffset, ring: Sequence[PlanarOffset]) -> bool:
    """Integer ray casting toward +east over a closed ring.

    Edges are tested half-open in northing, so a vertex shared by two edges is
    counted once; a point on an edge is inside, which is the conservative reading.
    """

    if _point_on_any_edge(p, ring):
        return True
    inside = False
    for a, b in _ring_edges(ring):
        dy = _n(b) - _n(a)
        if not (_n(a) <= _n(p) < _n(b)) and not (_n(b) <= _n(p) < _n(a)):
            continue
        left = (_e(p) - _e(a)) * dy
        right = (_n(p) - _n(a)) * (_e(b) - _e(a))
        crosses_east = right > left if dy > 0 else right < left
        if crosses_east:
            inside = not inside
    return inside


def _point_on_any_edge(p: PlanarOffset, ring: Sequence[PlanarOffset]) -> bool:
    return any(_orient(a, b, p) == 0 and _on_segment(p, a, b) for a, b in _ring_edges(ring))


def _ring_edges(ring: Sequence[PlanarOffset]) -> Iterator[tuple[PlanarOffset, PlanarOffset]]:
    """Consecutive edge pairs of a closed ring (the last vertex repeats the first)."""

    return ((ring[i], ring[i + 1]) for i in range(len(ring) - 1))


@dataclass(frozen=True)
class Footprint:
    """A restricted ground footprint: closed polygon, circle or buffered line.

    ``origin`` overrides the tangent-plane anchor. Queries re-anchor it to the
    midpoint of footprint and path so both project without distortion; the extent
    guard in :meth:`__post_init__` then refuses any comparison that is too wide to
    be decided on one plane, instead of quietly deciding it wrong.
    """

    shape: FootprintShape
    vertices: tuple[GeoPoint, ...]
    radius_mm: Millimetre | None = None
    width_mm: Millimetre | None = None
    origin: GeoPoint | None = None

    def __post_init__(self) -> None:
        if not self.vertices:
            raise GeometryInputError(f"{self.shape.value} footprint has no vertices")
        for point in self.vertices:
            if not isinstance(point, GeoPoint):
                raise GeometryInputError("footprint vertices must be GeoPoint values")
            if point.is_pole():
                raise GeometryInputError("a footprint vertex at a pole has no east direction")
        self._check_shape()
        self._check_extent()

    def _check_shape(self) -> None:
        if self.shape is FootprintShape.POLYGON:
            if len(self.vertices) < MIN_CLOSED_RING_POINTS:
                raise GeometryInputError(
                    f"a polygon needs a closed ring of at least {MIN_CLOSED_RING_POINTS} points"
                )
            if self.vertices[0] != self.vertices[-1]:
                raise GeometryInputError("a polygon ring must be closed: first point != last point")
            if self.radius_mm is not None or self.width_mm is not None:
                raise GeometryInputError("a polygon carries no radius or half-width")
        elif self.shape is FootprintShape.CIRCLE:
            if len(self.vertices) != 1:
                raise GeometryInputError("a circle has exactly one centre vertex")
            if self.radius_mm is None or self.radius_mm <= 0:
                raise GeometryInputError("a circle needs a positive radius")
            if self.width_mm is not None:
                raise GeometryInputError("a circle carries no buffered-line half-width")
        else:
            if len(self.vertices) < MIN_LINE_POINTS:
                raise GeometryInputError("a buffered line needs at least two points")
            if self.width_mm is None or self.width_mm <= 0:
                raise GeometryInputError("a buffered line needs a positive half-width")
            if self.radius_mm is not None:
                raise GeometryInputError("a buffered line carries no circle radius")

    def _check_extent(self) -> None:
        for point in self.vertices:
            if (
                abs(point.lat_deg - self.anchor.lat_deg) > 0.4
                or abs(point.lon_deg - self.anchor.lon_deg) > 0.4
            ):
                raise GeometryInputError(
                    f"footprint vertex ({point.lat_deg}, {point.lon_deg}) is outside the "
                    "0.4 deg tangent plane of this footprint: split the zone or anchor it "
                    "centrally rather than at a corner"
                )

    @property
    def anchor(self) -> GeoPoint:
        """The projection origin: an explicit re-anchor, else the first vertex."""

        return self.origin if self.origin is not None else self.vertices[0]

    def reanchored(self, origin: GeoPoint) -> Footprint:
        """The same footprint on a tangent plane centred on ``origin``."""

        return replace(self, origin=origin)

    @property
    def plane(self) -> LocalPlane:
        return LocalPlane(reference=self.anchor)

    @property
    def reach_mm(self) -> Millimetre:
        """Distance from the anchor that still could touch the footprint, plus slack."""

        spread = max(geodesic_distance_mm(self.anchor, point) for point in self.vertices)
        extra = max(self.radius_mm or 0, self.width_mm or 0)
        return Millimetre(spread + extra + PRE_FILTER_MARGIN_MM)

    def _ring(self) -> tuple[PlanarOffset, ...]:
        plane = self.plane
        return tuple(plane.offset_of(point) for point in self.vertices)

    def _project(self, point: GeoPoint) -> PlanarOffset | None:
        if geodesic_distance_mm(self.anchor, point) > self.reach_mm:
            return None
        try:
            return self.plane.offset_of(point)
        except Exception:
            return None

    def contains(self, point: GeoPoint) -> bool:
        """True when ``point`` is inside or on the boundary of the footprint."""

        offset = self._project(point)
        if offset is None:
            return False
        if self.shape is FootprintShape.CIRCLE:
            centre = self.plane.offset_of(self.vertices[0])
            return _within_distance(offset, centre, centre, int(self.radius_mm or 0))
        ring = self._ring()
        if self.shape is FootprintShape.POLYGON:
            return _point_in_ring(offset, ring)
        reach = int(self.width_mm or 0)
        return any(_within_distance(offset, a, b, reach) for a, b in _ring_edges(ring))

    def crossed_segments(self, path: Sequence[GeoPoint]) -> tuple[int, ...]:
        """Indices ``i`` of path segments ``(i, i+1)`` that enter or cross the footprint."""

        if len(path) < 2:
            raise GeometryInputError("a path needs at least two points")
        try:
            offsets = [self.plane.offset_of(point) for point in path]
        except Exception as error:
            raise GeometryInputError(
                f"path does not fit the projection of {self.shape.value} footprint: {error}"
            ) from error
        ring = self._ring() if self.shape is not FootprintShape.CIRCLE else ()
        return tuple(
            index
            for index in range(len(offsets) - 1)
            if self._segment_hits(offsets[index], offsets[index + 1], ring)
        )

    def _segment_hits(
        self, start: PlanarOffset, end: PlanarOffset, ring: Sequence[PlanarOffset]
    ) -> bool:
        if self.shape is FootprintShape.CIRCLE:
            centre = self.plane.offset_of(self.vertices[0])
            return _within_distance(centre, start, end, int(self.radius_mm or 0))
        if self.shape is FootprintShape.POLYGON:
            return (
                _point_in_ring(start, ring)
                or _point_in_ring(end, ring)
                or any(_segments_meet(start, end, a, b) for a, b in _ring_edges(ring))
            )
        reach = int(self.width_mm or 0)
        if _point_in_ring(start, ring) or _point_in_ring(end, ring):
            return True
        return any(_segments_within(start, end, a, b, reach) for a, b in _ring_edges(ring))

    def describe(self) -> str:
        """One-line description for a decision record."""

        if self.shape is FootprintShape.CIRCLE:
            centre = self.vertices[0]
            return (
                f"circle r={int(self.radius_mm or 0) / 1000:.0f} m at "
                f"({centre.lat_deg:.5f}, {centre.lon_deg:.5f})"
            )
        return f"{self.shape.value} over {len(self.vertices) - 1 or 1} spans"

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        """``(south, west, north, east)`` widened by the reach: a separation screen.

        The allowance is deliberately too wide: it uses the smallest plausible
        metres-per-degree and adds slack, so this screen can reject a far-away query
        but can never reject a near-miss that the exact predicate would have caught.
        """

        lats = [point.lat_deg for point in self.vertices]
        lons = [point.lon_deg for point in self.vertices]
        south, north = min(lats), max(lats)
        west, east = min(lons), max(lons)
        spread_m = self.reach_mm / 1000.0
        cos_min = min(math.cos(math.radians(abs(value))) for value in (south, north))
        d_lat = spread_m / MIN_METRES_PER_DEGREE + BBOX_SLACK_DEG
        d_lon = spread_m / (MIN_METRES_PER_DEGREE * max(cos_min, 1e-6)) + BBOX_SLACK_DEG
        return (south - d_lat, west - d_lon, north + d_lat, east + d_lon)

    def separated_from(self, path: Sequence[GeoPoint]) -> bool:
        """True only when the path's own box cannot reach the footprint's box.

        A straight segment stays inside the box of its endpoints, and that box is
        inside the box of all path points, so a disjoint pair of boxes is a proof of
        separation - which is why the boxes are expanded rather than tight.
        """

        if not path:
            raise GeometryInputError("no points to separate from a footprint")
        south, west, north, east = self.bbox
        lats = [point.lat_deg for point in path]
        lons = [point.lon_deg for point in path]
        return max(lats) < south or min(lats) > north or max(lons) < west or min(lons) > east


MIN_METRES_PER_DEGREE = 110_000.0
BBOX_SLACK_DEG = 0.01


def shared_origin(*groups: Sequence[GeoPoint]) -> GeoPoint:
    """Centre of the box enclosing every point that must share one plane."""

    points = [point for group in groups for point in group]
    if not points:
        raise GeometryInputError("no points to anchor a plane on")
    lats = [point.lat_deg for point in points]
    lons = [point.lon_deg for point in points]
    return GeoPoint((min(lats) + max(lats)) / 2.0, (min(lons) + max(lons)) / 2.0)


def comparable_footprint(footprint: Footprint, path: Sequence[GeoPoint]) -> Footprint:
    """Re-anchor a footprint so this path is evaluated on the same plane as its points.

    Raises:
        GeometryInputError: the combined extent is too wide for one tangent plane, in
            which case the caller must fail closed rather than guess at a projection.
    """

    return footprint.reanchored(shared_origin(footprint.vertices, path))
