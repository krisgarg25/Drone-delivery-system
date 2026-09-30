"""Footprint geometry. Shapely is touched in this module and nowhere else.

Every public function here takes and returns integer millimetres or WKT text, so
no shapely object - and therefore no untyped value - crosses into the kernel's
decision code. The swept-volume verdicts in :mod:`droneops_geom_4d.intersect`
are produced by exact integer/rational arithmetic; shapely geometry is used for
two jobs it is good at and that never decide safety on their own:

* building the horizontal footprint of a sweep (a capsule, i.e. a straight
  segment buffered by the protected radius, which is *exactly* the union of the
  moving disc over the segment), and
* rasterising a footprint onto a grid, for the cheap cell pre-filter.

Buffering is done with integer radii on integer coordinates, so the result is a
pure function of the inputs: the same profile on the same shapely build gives
the same WKT on every replay (conventions section 4).
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from shapely.geometry import LineString, MultiPoint, Point, box
from shapely.ops import unary_union
from shapely.wkt import loads as _loads


def disc_wkt(east_mm: int, north_mm: int, radius_mm: int) -> str:
    """The protected disc of a stationary body as WKT."""
    return str(Point(float(east_mm), float(north_mm)).buffer(float(radius_mm)).wkt)


def capsule_wkt(path: Sequence[tuple[int, int]], radius_mm: int) -> str:
    """Sweep a disc along a polyline: the exact continuous horizontal footprint.

    For a body that moves in a straight line from ``A`` to ``B``, the union of
    its protected discs over the whole leg *is* the capsule ``AB`` buffered by
    the radius - so this footprint adds nothing and loses nothing. That is what
    makes the continuous path defensible as an exact construction rather than an
    approximation with a smaller step size.
    """
    points = [(float(x), float(y)) for x, y in path]
    if len(points) == 1:
        return disc_wkt(path[0][0], path[0][1], radius_mm)
    return str(LineString(points).buffer(float(radius_mm)).wkt)


def hull_wkt(path: Sequence[tuple[int, int]]) -> str:
    """Smallest convex footprint around a path, for merge and crossing volumes."""
    points = [(float(x), float(y)) for x, y in path]
    if len(points) < 3:
        return capsule_wkt([(int(x), int(y)) for x, y in path], 0)
    return str(unary_union([Point(x, y).buffer(0.0) for x, y in points]).convex_hull.wkt)


def bounding_box(wkt: str) -> tuple[int, int, int, int]:
    """``(min_x, min_y, max_x, max_y)`` rounded *outward* to whole millimetres."""
    bounds = _loads(wkt).bounds
    return (
        math.floor(float(bounds[0])),
        math.floor(float(bounds[1])),
        math.ceil(float(bounds[2])),
        math.ceil(float(bounds[3])),
    )


def shapes_overlap(left_wkt: str, right_wkt: str) -> bool:
    """Conservative horizontal overlap: touching counts, because bodies do."""
    return bool(_loads(left_wkt).intersects(_loads(right_wkt)))


def box_overlap(wkt: str, min_x: int, min_y: int, max_x: int, max_y: int) -> bool:
    """True when the footprint touches a cell box, so no footprint slips between centres."""
    return bool(_loads(wkt).intersects(box(min_x, min_y, max_x, max_y)))


def contains_xy(wkt: str, east_mm: int, north_mm: int) -> bool:
    """Membership test used by the grid rasterisation."""
    return bool(_loads(wkt).covers(Point(float(east_mm), float(north_mm))))


def is_usable(wkt: str) -> bool:
    """Valid and non-empty. Anything else makes a proof impossible, not safe."""
    shape = _loads(wkt)
    return bool(shape.is_valid and not shape.is_empty)


def overlap_wkt(left_wkt: str, right_wkt: str) -> str:
    """The shared horizontal region, for the volume a merge or crossing occupies."""
    return str(_loads(left_wkt).intersection(_loads(right_wkt)).wkt)


def centroid_of(wkt: str) -> tuple[int, int]:
    """Rounded centroid of a footprint, for reporting a contact location."""
    centre = _loads(wkt).centroid
    return (round(float(centre.x)), round(float(centre.y)))


def multi_disc_wkt(centres: Sequence[tuple[int, int]], radius_mm: int) -> str:
    """Union of discs at several instants: the conservative per-frame footprint."""
    parts = [Point(float(x), float(y)).buffer(float(radius_mm)) for x, y in centres]
    if not parts:
        return str(MultiPoint([]).wkt)
    return str(unary_union(parts).wkt)
