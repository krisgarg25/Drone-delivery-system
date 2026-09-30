"""Shapely, touched in this module and nowhere else.

This is the second geometry tool the independence requirement asks for
(``technical-build/11`` section 7: "use different geometry tools"). The planner's
path rasterises swept prisms onto ``geom-core`` cells and decides contacts with
closed-form rational arithmetic over those prisms. This package decides contacts by
intersecting a *relative track* with a buffered disc in shapely, and by intersecting
two track polylines with each other - a library geometry kernel doing floating-point
planar topology where the planner does integer arithmetic on its own volume objects.

That swap buys independence and costs exactness, so the cost is paid for in the one
direction that matters: every radius handed to this module arrives already rounded
outward (see :class:`~droneops_conflict_checker.policy.CheckerPolicy`), and every
distance leaving here is rounded *up*. A float that is a hair too small can
therefore only ever make the checker say ``CONFLICT`` earlier, never ``SAFE`` later.

No shapely object crosses out of this module: coordinates go in as plain float
tuples, distances and points come out as plain floats and tuples, and everything
downstream is integers and text - so a shapely upgrade can change a number in a
verdict but never a type in the decision code.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from typing import Final

from shapely.geometry import LineString, Point

#: One point is not a line: a crossing test needs at least two vertices on each track.
_MIN_LINE_POINTS: Final[int] = 2


def segment_gap_mm(start: tuple[float, float], end: tuple[float, float]) -> float:
    """Minimum distance from the origin to a straight relative track.

    The argument is a *relative* segment: where body A is minus where body B is. Its
    distance from ``0, 0`` is the closest the two centres ever get laterally inside
    the window - and a continuous segment is exactly what a frame sample is not,
    which is why this answers the "fast aircraft passed through another one between
    two frames" case that per-frame checking cannot.
    """
    if start == end:
        return _round_up(float(math.hypot(start[0], start[1])))
    return _round_up(float(LineString([start, end]).distance(Point(0.0, 0.0))))


def segment_contact_span(
    start: tuple[float, float],
    end: tuple[float, float],
    reach_mm: float,
    window_start_us: int,
    window_end_us: int,
) -> tuple[int, int] | None:
    """The whole-microsecond range inside the window where the track is within reach.

    ``None`` when the relative track never enters the disc, which is the lateral
    clearance answer for this window. Otherwise the bounds are window instants
    rounded *outward* from the geometric crossing of the disc boundary, so a contact
    is never reported as starting later or ending sooner than it actually was.
    """
    if window_end_us < window_start_us:
        return None
    if start == end:
        inside = _round_up(float(math.hypot(start[0], start[1]))) <= reach_mm
        return (window_start_us, window_end_us) if inside else None
    line = LineString([start, end])
    cut = Point(0.0, 0.0).buffer(reach_mm).intersection(line)
    if bool(cut.is_empty):
        return None
    fractions = [_arc_fraction(start, end, point) for point in _coordinates(cut)]
    if not fractions:
        return (window_start_us, window_end_us)
    span = float(window_end_us - window_start_us)
    low = int(math.floor(window_start_us + min(fractions) * span))
    high = int(math.ceil(window_start_us + max(fractions) * span))
    return (max(window_start_us, low), min(window_end_us, high))


def closest_approach_us(
    start: tuple[float, float],
    end: tuple[float, float],
    window_start_us: int,
    window_end_us: int,
) -> int:
    """The instant of least lateral separation, for reporting where a contact began.

    Projection along the track is arc length, and the track is parameterised linearly
    in time, so a fraction of length is the same fraction of the window.
    """
    if start == end or window_end_us <= window_start_us:
        return window_start_us
    return int(
        round(window_start_us + _arc_fraction(start, end, (0.0, 0.0)) *
              float(window_end_us - window_start_us))
    )


def polyline_gap_mm(
    left: Sequence[tuple[float, float]], right: Sequence[tuple[float, float]]
) -> float:
    """Minimum planar distance between two track polylines, ignoring time entirely.

    This is deliberately the coarser question the adjacent-corridor inspection asks:
    not "were they ever here together" but "do these two lanes touch at all".
    Answering it sacrifices nothing but false positives, which is precisely the trade
    this package is required to make.
    """
    if not left or not right:
        return math.inf
    return _round_up(float(_shape(left).distance(_shape(right))))


def polyline_crossings(
    left: Sequence[tuple[float, float]], right: Sequence[tuple[float, float]]
) -> tuple[tuple[float, float], ...]:
    """Every point where the two horizontal tracks cross, in ascending order.

    A shared endpoint counts as a crossing, because two lanes that meet at a node are
    two lanes that meet; whether that contact is *permitted* is the corridor graph's
    and the policy's business, not a geometry module's.
    """
    if len(left) < _MIN_LINE_POINTS or len(right) < _MIN_LINE_POINTS:
        return ()
    shared = LineString(list(left)).intersection(LineString(list(right)))
    if bool(shared.is_empty):
        return ()
    return tuple(sorted({(round(x, 6), round(y, 6)) for x, y in _coordinates(shared)}))


def _shape(track: Sequence[tuple[float, float]]) -> Point | LineString:
    """A polyline for a moving track, a point for a stationary body."""
    if len(track) == 1:
        return Point(track[0][0], track[0][1])
    return LineString(list(track))


def _arc_fraction(
    start: tuple[float, float], end: tuple[float, float], point: tuple[float, float]
) -> float:
    """Where along a relative track a point projects to, as a clamped ``[0, 1]`` fraction."""
    line = LineString([start, end])
    length = float(line.length)
    if length <= 0.0:
        return 0.0
    return _clamp(float(line.project(Point(point[0], point[1]))) / length)


def _coordinates(geometry: object) -> tuple[tuple[float, float], ...]:
    """``(east, north)`` of every vertex of a shapely geometry, as plain floats.

    A contact can be a point, a line, or a multipart result of two overlapping
    tracks, and shapely refuses ``.coords`` on a multipart and ``.geoms`` on a
    single-part one. Both refusals are caught rather than predicted: a geometry
    type that appears in a future release degrades to "no vertices here", which
    the callers treat as no crossing, never as a clearance.
    """
    found: list[tuple[float, float]] = []
    try:
        # Deliberately dynamic: shapely raises when ``coords`` is read off a multipart
        # and when ``geoms`` is read off a single-part geometry, and an attribute the
        # library itself decides to withhold cannot be accessed with a plain ``.attr``.
        coords: Iterable[object] | None = getattr(geometry, "coords")  # noqa: B009
    except (AttributeError, NotImplementedError):
        coords = None
    if coords is not None:
        for vertex in coords:
            if isinstance(vertex, (tuple, list)) and len(vertex) >= 2:
                found.append((float(vertex[0]), float(vertex[1])))
    try:
        parts: Iterable[object] = getattr(geometry, "geoms")  # noqa: B009
    except (AttributeError, NotImplementedError):
        parts = ()
    for part in parts:
        found.extend(_coordinates(part))
    return tuple(found)


def _clamp(fraction: float) -> float:
    """Keep an arc-length fraction inside ``[0, 1]``: shapely clamps, and so do we."""
    return 0.0 if fraction < 0.0 else (1.0 if fraction > 1.0 else fraction)


def _round_up(value: float) -> float:
    """Round a reported distance up to micrometre resolution.

    A gap may be overstated and may never be understated, so the float path can only
    widen a conflict and can never narrow a clearance.
    """
    if math.isinf(value) or math.isnan(value):
        return value
    return float(math.ceil(value * 1000.0) / 1000.0)
