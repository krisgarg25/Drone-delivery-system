"""``RouteProfile``: a body's position, altitude and speed as a function of time.

This is the input every swept volume is built from, and the reason the sweep can
be *continuous*: a profile is a law, not a list of frames. Between two waypoints
the body is somewhere, so the protected volume between two waypoints is
something, and a checker that only inspects waypoints cannot say where the body
was at the instants in between (``01-technical-architecture.md`` section 8).

Interpolation model, stated once:
    * time is integer microseconds and **strictly monotonic** - a repeated or
      reversed timestamp is rejected at construction, never normalised, because
      a profile whose clock repeats would interpolate a place the aircraft never
      occupied;
    * horizontal position is linear in time on one projected local plane, in
      integer millimetres;
    * altitude is linear in time in integer millimetres **within one reference
      frame**; a profile that mixes ``AGL`` and ``AMSL`` waypoints is rejected
      outright, because the datum difference between them is exactly the thing
      that would have to be guessed (invariant I9);
    * the *declared* waypoint speed is metadata for reporting and for bounding a
      discretisation. The geometry a conflict verdict rests on comes from the
      positions themselves, so an under-declared speed cannot shrink a protected
      volume. A profile that declares zero speed at both ends of a leg that
      plainly moves is a contradiction and is rejected.

Exactness rule: interpolation at an arbitrary instant is a rational number with
no rounding at all (see :func:`exact_axis`), and the integer view of it
(:func:`quantised_axis`) floors, so a quantised position is never ahead of the
truth. The decision procedure in :mod:`droneops_geom_4d.intersect` works on the
exact rationals and never inherits that rounding.

Provenance: ``authoritative``, ``source`` and ``plan_version`` travel with the
profile and are copied into every volume and verdict built from it. A synthetic
plan can produce a verdict, but never an authoritative one.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Final

from droneops_contracts.geometry import Position
from droneops_contracts.route import CoordinateTimeProfile, RoutePoint
from droneops_geom_core import (
    MICROS_PER_SECOND,
    Altitude,
    AltitudeBand,
    AltitudeReference,
    GeomCoreError,
    GeoPoint,
    Interval,
    LocalPlane,
    Microsecond,
    Millimetre,
    PlanarOffset,
    as_microseconds,
    as_millimetres,
    metres_to_millimetres,
    reference_from_token,
)

from .errors import ProfileError

#: A profile shorter than this cannot express motion, so it cannot be swept.
MIN_POINTS: Final[int] = 2
#: Ceiling on profile size, so one malformed plan cannot exhaust an evaluation run.
MAX_POINTS: Final[int] = 4096
MICROS_PER_S: Final[int] = MICROS_PER_SECOND


def _ceil_sqrt(value: int) -> int:
    """Smallest integer whose square is ``>= value``: a distance bound, never short."""
    root = math.isqrt(value)
    return root if root * root == value else root + 1


def exact_axis(
    at_us: int, start_us: int, end_us: int, start_value: int, end_value: int
) -> Fraction:
    """Linear interpolation as an exact rational: no float, no rounding, no drift.

    Outside the segment the value is clamped to the near endpoint, which is what
    "the body stayed where the plan put it" means at the padded ends of a sweep.
    """
    if end_us <= start_us:
        raise ProfileError(f"segment span must be positive: {start_us} -> {end_us}")
    if at_us <= start_us:
        return Fraction(start_value)
    if at_us >= end_us:
        return Fraction(end_value)
    elapsed = Fraction(at_us - start_us, end_us - start_us)
    return Fraction(start_value) + elapsed * Fraction(end_value - start_value)


def quantised_axis(
    at_us: int, start_us: int, end_us: int, start_value: int, end_value: int
) -> Millimetre:
    """The exact value floored to a whole unit, so it never overstates progress."""
    return as_millimetres(math.floor(exact_axis(at_us, start_us, end_us, start_value, end_value)))


def _reference_of(position: Position) -> AltitudeReference:
    """Contract reference to kernel reference, by token, never by assumption."""
    token = position.altitude_reference.value
    try:
        return reference_from_token(token)
    except GeomCoreError as exc:
        raise ProfileError(
            f"position carries unusable altitude reference {token!r} (invariant I9)"
        ) from exc


@dataclass(frozen=True)
class ProfilePoint:
    """One waypoint: an instant, a place, a height with its frame and a speed."""

    at_us: Microsecond
    position: GeoPoint
    altitude: Altitude
    speed_mmps: int

    def __post_init__(self) -> None:
        if self.speed_mmps < 0:
            raise ProfileError(f"speed cannot be negative, got {self.speed_mmps} mm/s")

    @classmethod
    def at(  # noqa: PLR0917 - positional waypoint shorthand for tests
        cls,
        at_us: int,
        latitude_deg: float,
        longitude_deg: float,
        altitude_mm: int,
        reference: AltitudeReference,
        speed_mmps: int,
    ) -> ProfilePoint:
        """Ergonomic constructor that keeps a height glued to its reference."""
        return cls(
            Microsecond(at_us),
            GeoPoint(latitude_deg, longitude_deg),
            Altitude(reference, as_millimetres(altitude_mm)),
            speed_mmps,
        )

    @classmethod
    def from_contract(cls, point: RoutePoint, *, at_us: int) -> ProfilePoint:
        """Build a kernel point from a ``contracts`` route point (metres -> mm once)."""
        position = point.position
        return cls(
            at_us=as_microseconds(at_us),
            position=GeoPoint(position.lat, position.lon),
            altitude=Altitude(_reference_of(position), metres_to_millimetres(position.altitude_m)),
            speed_mmps=metres_to_millimetres(point.speed_ms),
        )

    @property
    def altitude_reference(self) -> AltitudeReference:
        return self.altitude.reference


@dataclass(frozen=True)
class ProfileSegment:
    """The law between two consecutive waypoints, with its projected offsets."""

    index: int
    start: ProfilePoint
    end: ProfilePoint
    start_offset: PlanarOffset
    end_offset: PlanarOffset

    def __post_init__(self) -> None:
        if self.end.at_us <= self.start.at_us:
            raise ProfileError(f"segment {self.index} runs backwards in time")

    @property
    def window(self) -> Interval:
        """Half-open occupancy window ``[start, end)``."""
        return Interval(self.start.at_us, self.end.at_us)

    @property
    def duration_us(self) -> Microsecond:
        return Microsecond(self.end.at_us - self.start.at_us)

    @property
    def chord_length_mm(self) -> Millimetre:
        """Straight-line distance flown, rounded *up*: a bound, never an undercount."""
        dx = self.end_offset.east_mm - self.start_offset.east_mm
        dy = self.end_offset.north_mm - self.start_offset.north_mm
        return Millimetre(_ceil_sqrt(dx * dx + dy * dy))

    @property
    def speed_mm_per_us(self) -> Fraction:
        """Implied ground speed of the chord: what the positions themselves say."""
        return Fraction(self.chord_length_mm, self.duration_us)

    @property
    def vertical_rate_mm_per_us(self) -> Fraction:
        """Signed climb (positive) or descent (negative) rate per microsecond."""
        return Fraction(self.end.altitude.value_mm - self.start.altitude.value_mm, self.duration_us)

    @property
    def altitude_range_mm(self) -> tuple[int, int]:
        return (
            min(self.start.altitude.value_mm, self.end.altitude.value_mm),
            max(self.start.altitude.value_mm, self.end.altitude.value_mm),
        )

    @property
    def is_vertical_change(self) -> bool:
        return self.start.altitude.value_mm != self.end.altitude.value_mm

    def position_at(self, at_us: int) -> tuple[Millimetre, Millimetre]:
        """Interpolated ``(east, north)`` in whole millimetres."""
        return (
            quantised_axis(
                at_us,
                self.start.at_us,
                self.end.at_us,
                self.start_offset.east_mm,
                self.end_offset.east_mm,
            ),
            quantised_axis(
                at_us,
                self.start.at_us,
                self.end.at_us,
                self.start_offset.north_mm,
                self.end_offset.north_mm,
            ),
        )

    def altitude_at(self, at_us: int) -> Millimetre:
        return quantised_axis(
            at_us,
            self.start.at_us,
            self.end.at_us,
            self.start.altitude.value_mm,
            self.end.altitude.value_mm,
        )

    def exact_position_at(self, at_us: int) -> tuple[Fraction, Fraction]:
        """Interpolated ``(east, north)`` as rationals: what the sweep decides on."""
        return (
            exact_axis(
                at_us,
                self.start.at_us,
                self.end.at_us,
                self.start_offset.east_mm,
                self.end_offset.east_mm,
            ),
            exact_axis(
                at_us,
                self.start.at_us,
                self.end.at_us,
                self.start_offset.north_mm,
                self.end_offset.north_mm,
            ),
        )

    def exact_altitude_at(self, at_us: int) -> Fraction:
        return exact_axis(
            at_us,
            self.start.at_us,
            self.end.at_us,
            self.start.altitude.value_mm,
            self.end.altitude.value_mm,
        )


@dataclass(frozen=True)
class ProfileState:
    """The body's reported state at one instant."""

    at_us: Microsecond
    east_mm: Millimetre
    north_mm: Millimetre
    altitude_mm: Millimetre
    reference: AltitudeReference
    speed_mmps: int

    @property
    def offset(self) -> PlanarOffset:
        return PlanarOffset(self.east_mm, self.north_mm)

    @property
    def altitude(self) -> Altitude:
        return Altitude(self.reference, self.altitude_mm)


def _build_segments(
    points: Sequence[ProfilePoint], plane: LocalPlane
) -> tuple[ProfileSegment, ...]:
    """Project the waypoints once and pair them in waypoint order."""
    offsets = tuple(plane.offset_of(point.position) for point in points)
    return tuple(
        ProfileSegment(index, points[index], points[index + 1], offsets[index], offsets[index + 1])
        for index in range(len(points) - 1)
    )


@dataclass(frozen=True)
class RouteProfile:
    """A time-ordered sequence of waypoints plus the plane they are projected on."""

    points: tuple[ProfilePoint, ...]
    plane: LocalPlane
    plan_version: str
    source: str
    authoritative: bool = False
    _segments: tuple[ProfileSegment, ...] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        ordered = tuple(self.points)
        if len(ordered) < MIN_POINTS:
            raise ProfileError(
                f"a profile needs at least {MIN_POINTS} waypoints, got {len(ordered)}"
            )
        if len(ordered) > MAX_POINTS:
            raise ProfileError(f"profile exceeds the {MAX_POINTS}-waypoint ceiling")
        if not self.plan_version.strip() or not self.source.strip():
            raise ProfileError("plan_version and source are required provenance")
        first_reference = ordered[0].altitude.reference
        previous: Microsecond | None = None
        for point in ordered:
            if point.altitude.reference is not first_reference:
                raise ProfileError(
                    "a profile must stay in one altitude reference: mixing "
                    f"{first_reference.value} and {point.altitude.reference.value} would "
                    "invent a datum difference"
                )
            if previous is not None and point.at_us <= previous:
                raise ProfileError(
                    f"waypoint times must be strictly monotonic: {point.at_us} follows {previous}"
                )
            previous = point.at_us
        built = _build_segments(ordered, self.plane)
        for segment in built:
            silent = segment.start.speed_mmps == 0 and segment.end.speed_mmps == 0
            if silent and segment.chord_length_mm > 0:
                raise ProfileError(
                    f"segment {segment.index} declares zero speed at both ends yet moves "
                    f"{segment.chord_length_mm} mm: positions and speeds must agree"
                )
        object.__setattr__(self, "_segments", built)

    # --- construction ----------------------------------------------------------

    @classmethod
    def from_points(
        cls,
        points: Sequence[ProfilePoint],
        *,
        plan_version: str,
        source: str,
        authoritative: bool = False,
        plane: LocalPlane | None = None,
    ) -> RouteProfile:
        """Build a profile, projecting it on the first waypoint's local plane.

        Pass ``plane`` explicitly whenever two profiles are going to be compared:
        coordinates are only meaningful inside one projected frame, and the
        intersection kernel refuses prisms that were projected from different
        origins. Defaulting to the first waypoint is right for a lone plan and
        wrong for a pair of them.
        """
        ordered = tuple(points)
        if not ordered:
            raise ProfileError("cannot build a profile from no waypoints")
        return cls(
            points=ordered,
            plane=plane if plane is not None else LocalPlane(ordered[0].position),
            plan_version=plan_version,
            source=source,
            authoritative=authoritative,
        )

    @classmethod
    def from_contract_profile(
        cls,
        route: CoordinateTimeProfile,
        *,
        start_us: int,
        plan_version: str,
        source: str,
        authoritative: bool = False,
    ) -> RouteProfile:
        """Adopt a ``contracts`` ``coordinate-time-profile`` as a kernel profile.

        ``RoutePoint.t`` is integer seconds from plan start while the kernel
        clock is absolute integer microseconds, so the origin is supplied by the
        caller instead of being read from a clock.
        """
        origin = as_microseconds(start_us)
        points = tuple(
            ProfilePoint.from_contract(point, at_us=origin + point.t * MICROS_PER_S)
            for point in route.points
        )
        return cls.from_points(
            points, plan_version=plan_version, source=source, authoritative=authoritative
        )

    # --- structure -------------------------------------------------------------

    @property
    def reference(self) -> AltitudeReference:
        """The single altitude reference of the whole profile (invariant I9)."""
        return self.points[0].altitude.reference

    @property
    def window(self) -> Interval:
        """Half-open occupancy window of the whole plan."""
        return Interval(self.points[0].at_us, self.points[-1].at_us)

    @property
    def duration_us(self) -> Microsecond:
        return Microsecond(self.window.measure)

    @property
    def max_speed_mm_per_us(self) -> Fraction:
        """Fastest implied chord speed: the bound a discretisation must respect."""
        return max((segment.speed_mm_per_us for segment in self._segments), default=Fraction(0))

    @property
    def max_vertical_rate_mm_per_us(self) -> Fraction:
        """Steepest implied climb or descent rate, as a magnitude."""
        return max(
            (abs(segment.vertical_rate_mm_per_us) for segment in self._segments),
            default=Fraction(0),
        )

    @property
    def altitude_band(self) -> AltitudeBand:
        """The vertical extent flown, in the profile's own reference."""
        heights = [point.altitude.value_mm for point in self.points]
        return AltitudeBand(
            self.reference, as_millimetres(min(heights)), as_millimetres(max(heights))
        )

    def segments(self) -> tuple[ProfileSegment, ...]:
        """Segments in waypoint order - which is also the sweep order."""
        return self._segments

    def segment_at(self, at_us: int) -> ProfileSegment:
        """The segment containing ``at_us``; the last segment owns its end instant."""
        if at_us < self.points[0].at_us or at_us > self.points[-1].at_us:
            raise ProfileError(f"{at_us} us is outside the profile window {self.window}")
        for segment in self._segments:
            if segment.start.at_us <= at_us < segment.end.at_us:
                return segment
        return self._segments[-1]

    # --- interpolation ---------------------------------------------------------

    def state_at(self, at_us: int) -> ProfileState:
        """Interpolated position, altitude and declared speed at an instant.

        Raises:
            ProfileError: the instant is outside ``[first, last]``. Interpolating
                past the end of a plan would extend a protected volume the plan
                never reserved.
        """
        segment = self.segment_at(at_us)
        east, north = segment.position_at(at_us)
        return ProfileState(
            at_us=Microsecond(at_us),
            east_mm=east,
            north_mm=north,
            altitude_mm=segment.altitude_at(at_us),
            reference=self.reference,
            speed_mmps=quantised_axis(
                at_us,
                segment.start.at_us,
                segment.end.at_us,
                segment.start.speed_mmps,
                segment.end.speed_mmps,
            ),
        )

    def position_at(self, at_us: int) -> GeoPoint:
        """Geodetic position at an instant, through the profile's own plane."""
        return self.plane.point_at(self.state_at(at_us).offset)

    def altitude_at(self, at_us: int) -> Altitude:
        return self.state_at(at_us).altitude

    def footprint_offsets(self) -> tuple[tuple[int, int], ...]:
        """Projected waypoint offsets in waypoint order: the polyline to sweep."""
        head = tuple(
            (segment.start_offset.east_mm, segment.start_offset.north_mm)
            for segment in self._segments
        )
        tail = self._segments[-1].end_offset
        return (*head, (tail.east_mm, tail.north_mm))
