"""From geodesy to route legs: the geom-core bridge.

The consumption kernel takes integer millimetre legs and integer millidegree
courses; it never sees a latitude or a longitude. This module is the boundary where
a corridor polyline becomes that input, using geom-core's own primitives so the
energy model measures a route the same way the planner and the conflict checker do
rather than inventing a second distance formula.

Bearing and distance come back from geom-core as doubles and are quantised here -
to millidegrees and integer millimetres - before any energy is computed, so the
floats stop at this file.
"""

from __future__ import annotations

from collections.abc import Sequence

from droneops_geom_core import GeoPoint, geodesic_distance_mm, initial_bearing_deg

from .errors import EnergyInputError
from .model import Leg, Route
from .numeric import require
from .trig import normalize_mdeg

#: Degrees to millidegrees.
MDEG_PER_DEG = 1000


def course_of(left: GeoPoint, right: GeoPoint) -> int:
    """The ground-track course of one segment, in normalised millidegrees."""
    if left.sort_key() == right.sort_key():
        raise EnergyInputError("a zero-length segment has no course; drop it or hover instead")
    return normalize_mdeg(int(round(initial_bearing_deg(left, right) * MDEG_PER_DEG)))


def distance_mm_of(left: GeoPoint, right: GeoPoint) -> int:
    """The integer millimetre length of one segment, from geom-core's measure."""
    return int(geodesic_distance_mm(left, right))


def leg_between(
    origin: GeoPoint,
    destination: GeoPoint,
    *,
    climb_mm: int = 0,
    descent_mm: int = 0,
    hover_us: int = 0,
) -> Leg:
    """One geodesic segment as a costing leg."""
    return Leg(
        distance_mm=distance_mm_of(origin, destination),
        course_mdeg=course_of(origin, destination),
        climb_mm=climb_mm,
        descent_mm=descent_mm,
        hover_us=hover_us,
    )


def climb_and_descent_mm(heights_mm: Sequence[int]) -> tuple[int, int]:
    """Total ascent and descent of a height profile, in integer millimetres.

    Only the changes are summed, not the absolute heights: a route that climbs 40 m
    then descends 40 m has both terms at 40 m, which is what the climb-cost and
    descent-recovery curves are priced against.
    """
    require(len(heights_mm) >= 2, "a height profile needs at least two points")
    climb = 0
    descent = 0
    for lower, upper in zip(heights_mm, heights_mm[1:], strict=False):
        delta = int(upper) - int(lower)
        if delta >= 0:
            climb += delta
        else:
            descent -= delta
    return climb, descent


def route_from_points(
    label: str,
    points: Sequence[GeoPoint],
    *,
    heights_mm: Sequence[int] | None = None,
    hover_us: int = 0,
) -> Route:
    """A corridor polyline into the legs the energy model costs.

    ``heights_mm``, when supplied, must have one entry per point; the profile's
    total ascent and descent are then charged to the first leg, which is where a
    climb out of a pad actually happens.
    """
    if len(points) < 2:
        raise EnergyInputError(f"{label}: a route needs at least two points to have a length")
    if heights_mm is not None and len(heights_mm) != len(points):
        raise EnergyInputError(
            f"{label}: {len(heights_mm)} heights for {len(points)} points"
        )
    climb_mm, descent_mm = (0, 0) if heights_mm is None else climb_and_descent_mm(heights_mm)
    legs = tuple(
        leg_between(
            points[index],
            points[index + 1],
            climb_mm=climb_mm if index == 0 else 0,
            descent_mm=descent_mm if index == len(points) - 2 else 0,
            hover_us=hover_us if index == len(points) - 1 else 0,
        )
        for index in range(len(points) - 1)
    )
    return Route(label=label, legs=legs)


def route_distance_mm(route: Route) -> int:
    """The route's ground distance in integer millimetres."""
    return route.distance_mm
