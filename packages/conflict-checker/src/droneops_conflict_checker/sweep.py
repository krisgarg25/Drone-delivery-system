"""The checker's own protected-volume sweep, rebuilt from raw plan points.

``technical-build/11`` section 7 asks for exactly this file: *"recompute protected
volumes from raw plan points"*. Not from a planner's edge list, not from a corridor
graph's lanes, and not from ``droneops_geom_4d``'s prisms - the contract in
``.importlinter`` forbids that import and this module is where the forbidden work
actually gets done, from the geodetic points in the ledger row up.

The model, stated once
    A plan is a sequence of absolute instants and the places the aircraft was
    claimed to be at them. Between two consecutive points the centre moves in a
    straight line on one projected local plane, at a constant rate in each axis,
    and the protected volume is the cylinder of the body's reach around that
    centre. Each leg then makes three separate claims on space-time, and the
    difference between them is the whole reason this is a sweep and not a list:

    ``MOTION``
        the leg itself, over ``[t0, t1]``.
    ``HOLD_BEFORE`` / ``HOLD_AFTER``
        the same position held across the leg's padded window, because timing
        uncertainty says the aircraft may be *late to arrive* or *early to leave*.
        The planner pads timing uncertainty on every leg and the transition buffer
        only on a leg that changes height; the checker pads both on every leg, so a
        level pass that runs late is still protected. That asymmetry is intentional
        and it is one of the ways this implementation is allowed to be wrong in the
        safe direction.

    Every piece carries its own lateral reach and vertical half-extent, computed
    from the versioned policy at the time the sweep is built, so a verdict can be
    traced back to the policy release that produced it.

Why a *relative* track
    Two bodies moving linearly have a relative position that is also linear, so the
    set of lateral separations over a shared window is one straight segment and its
    distance from the origin is the closest they ever got. That single trick is what
    makes the check continuous: there is no step size, no frame interval, and no way
    for a fast aircraft to be on one side of another at sample *n* and on the other
    side at sample *n+1* without the segment crossing the disc in between. It is also
    a completely different construction from the planner's closed-form prism scan,
    which is the point of having two of them.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from fractions import Fraction

from droneops_geom_core import (
    MICROS_PER_SECOND,
    AltitudeReference,
    GeomCoreError,
    GeoPoint,
    Interval,
    LocalPlane,
)

from .policy import CheckerPolicy
from .records import (
    CommittedPlanRecord,
    PlanPoint,
    absolute_us,
    altitude_reference_of,
    point_altitude_mm,
)


class PieceKind(StrEnum):
    """What kind of claim a piece makes on space-time."""

    MOTION = "MOTION"
    HOLD_BEFORE = "HOLD_BEFORE"
    HOLD_AFTER = "HOLD_AFTER"
    SAMPLE = "SAMPLE"


class SweepError(ValueError):
    """This pair of records cannot be turned into geometry at all."""


@dataclass(frozen=True)
class TrackPoint:
    """One plan point, projected: integer microseconds and integer millimetres."""

    at_us: int
    east_mm: int
    north_mm: int
    altitude_mm: int


@dataclass(frozen=True)
class PairFrame:
    """The single projected plane two plans are compared on.

    The origin is the geodetically smallest point of the *pair*, chosen by
    ``geom-core``'s documented ``(lat, lon)`` sort key. Not the left plan's first
    point, not the earlier plan's: a verdict that changed when the arguments were
    swapped would be a verdict about argument order, and the symmetry property test
    would be false.
    """

    origin: GeoPoint
    plane: LocalPlane

    @classmethod
    def of(cls, *records: CommittedPlanRecord) -> PairFrame:
        """Build the shared frame, or raise when the two plans cannot share one."""
        candidates = [GeoPoint(point.lat_deg, point.lon_deg) for record in records for point in record.points]
        if not candidates:
            raise SweepError("neither record has a position to project")
        origin = min(candidates, key=GeoPoint.sort_key)
        frame = cls(origin, LocalPlane(origin))
        for record in records:
            for point in record.points:
                frame.offset_of(GeoPoint(point.lat_deg, point.lon_deg))
        return frame

    def offset_of(self, point: GeoPoint) -> tuple[int, int]:
        """Geodetic to planar millimetres, once, at the boundary of the record."""
        try:
            offset = self.plane.offset_of(point)
        except GeomCoreError as error:
            raise SweepError(str(error)) from error
        return (int(offset.east_mm), int(offset.north_mm))


@dataclass(frozen=True)
class PositionPiece:
    """One straight-line claim on space-time, with the reach that body carries.

    ``start_us`` and ``end_us`` are the first and last whole instants the piece is
    alive on, inclusive; the padded window a leg claims is derived from them at
    :meth:`window`. The centre is linear between the two endpoints, which are equal
    for a piece that only holds a position.
    """

    leg_index: int
    sample_index: int
    kind: PieceKind
    start_us: int
    end_us: int
    east_start: int
    north_start: int
    alt_start: int
    east_end: int
    north_end: int
    alt_end: int
    reach_mm: int
    vertical_half_mm: int

    def __post_init__(self) -> None:
        if self.end_us < self.start_us:
            raise SweepError(f"piece {self.kind.value}:{self.leg_index} runs backwards in time")
        if self.reach_mm <= 0 or self.vertical_half_mm <= 0:
            raise SweepError(
                f"piece {self.kind.value}:{self.leg_index} has a non-positive extent: "
                f"{self.reach_mm} mm lateral, {self.vertical_half_mm} mm vertical"
            )

    def window(self) -> Interval:
        """The half-open absolute interval this piece is alive on."""
        return Interval.of(self.start_us, self.end_us + 1)

    def sort_key(self) -> tuple[int, str, int, int]:
        """Documented order: leg, then kind, then sample, then instant."""
        return (self.leg_index, self.kind.value, self.sample_index, self.start_us)

    def centre_at(self, at_us: int) -> tuple[Fraction, Fraction, Fraction]:
        """Exact ``(east, north, altitude)`` of the centre at an instant, no rounding.

        Rational interpolation is kept for the vertical axis, where a millimetre of
        over- or under-statement is the answer, while the lateral axis is handed to
        shapely as a float and covered by outward rounding instead.
        """
        span = self.end_us - self.start_us
        if span <= 0 or at_us <= self.start_us:
            return (Fraction(self.east_start), Fraction(self.north_start), Fraction(self.alt_start))
        if at_us >= self.end_us:
            return (Fraction(self.east_end), Fraction(self.north_end), Fraction(self.alt_end))
        step = Fraction(at_us - self.start_us, span)
        return (
            Fraction(self.east_start) + step * (self.east_end - self.east_start),
            Fraction(self.north_start) + step * (self.north_end - self.north_start),
            Fraction(self.alt_start) + step * (self.alt_end - self.alt_start),
        )

    def travel_mm_per_us(self) -> float:
        """Planar speed along this piece, as a float for the inflation bound only."""
        span = max(1, self.end_us - self.start_us)
        return math.hypot(self.east_end - self.east_start, self.north_end - self.north_start) / span


@dataclass(frozen=True)
class SweptPlan:
    """A record turned into the geometry the checker reasons about."""

    plan_version: str
    reference: AltitudeReference
    pieces: tuple[PositionPiece, ...]
    track_mm: tuple[tuple[int, int], ...]
    corridor_keys: tuple[str, ...]
    window: Interval
    policy_key: str

    def sorted_pieces(self) -> tuple[PositionPiece, ...]:
        """Pieces in the documented order, never in ``set`` or ``dict`` order."""
        return tuple(sorted(self.pieces, key=PositionPiece.sort_key))

    def float_track(self) -> tuple[tuple[float, float], ...]:
        """The plan-level horizontal polyline, for the crossing and corridor tests."""
        return tuple((float(east), float(north)) for east, north in self.track_mm)

    def padded_window_us(self) -> tuple[int, int]:
        """Earliest and latest whole instant this plan's protection is alive on."""
        return (self.window.start, self.window.end - 1)


def _instant(point: PlanPoint) -> int:
    value = absolute_us(point.at)
    if value is None:
        raise SweepError(f"plan point at {point.at!r} has no UTC offset")
    return value


def _track(record: CommittedPlanRecord, frame: PairFrame) -> tuple[TrackPoint, ...]:
    points: list[TrackPoint] = []
    for point in record.points:
        east, north = frame.offset_of(GeoPoint(point.lat_deg, point.lon_deg))
        points.append(
            TrackPoint(
                at_us=_instant(point),
                east_mm=east,
                north_mm=north,
                altitude_mm=point_altitude_mm(point.altitude_m),
            )
        )
    return tuple(points)


def _reference(record: CommittedPlanRecord) -> AltitudeReference:
    reference = altitude_reference_of(record)
    if reference is None:
        raise SweepError(f"{record.plan_version} has no single altitude reference")
    return reference


def _motion_pieces(
    record: CommittedPlanRecord, track: tuple[TrackPoint, ...], policy: CheckerPolicy
) -> tuple[PositionPiece, ...]:
    """The exact continuous claim: motion plus a held tail and head on every leg.

    The pad is :meth:`CheckerPolicy.leg_pad_us`, applied without exception, and the
    reach is the body radius plus the policy's lateral margins.
    """
    pad = policy.leg_pad_us()
    reach = policy.lateral_reach_mm(record.body_radius_mm)
    vertical_half = policy.vertical_reach_mm(record.body_half_height_mm)
    pieces: list[PositionPiece] = []
    for index in range(len(track) - 1):
        first, second = track[index], track[index + 1]
        pieces.append(
            PositionPiece(
                leg_index=index, sample_index=0, kind=PieceKind.HOLD_BEFORE,
                start_us=first.at_us - pad, end_us=first.at_us,
                east_start=first.east_mm, north_start=first.north_mm, alt_start=first.altitude_mm,
                east_end=first.east_mm, north_end=first.north_mm, alt_end=first.altitude_mm,
                reach_mm=reach, vertical_half_mm=vertical_half,
            )
        )
        pieces.append(
            PositionPiece(
                leg_index=index, sample_index=0, kind=PieceKind.MOTION,
                start_us=first.at_us, end_us=second.at_us,
                east_start=first.east_mm, north_start=first.north_mm, alt_start=first.altitude_mm,
                east_end=second.east_mm, north_end=second.north_mm, alt_end=second.altitude_mm,
                reach_mm=reach, vertical_half_mm=vertical_half,
            )
        )
        pieces.append(
            PositionPiece(
                leg_index=index, sample_index=0, kind=PieceKind.HOLD_AFTER,
                start_us=second.at_us, end_us=second.at_us + pad,
                east_start=second.east_mm, north_start=second.north_mm, alt_start=second.altitude_mm,
                east_end=second.east_mm, north_end=second.north_mm, alt_end=second.altitude_mm,
                reach_mm=reach, vertical_half_mm=vertical_half,
            )
        )
    return tuple(pieces)


def _sampled_pieces(
    record: CommittedPlanRecord,
    track: tuple[TrackPoint, ...],
    policy: CheckerPolicy,
    step_us: int,
    *,
    inflate: bool,
) -> tuple[PositionPiece, ...]:
    """Stationary claims on a fixed grid: ``inflate`` decides which of two very different things this is.

    With ``inflate=True`` each sample's reach grows by the whole distance the body can
    travel inside the step, and the samples run across the leg's **padded** window, not
    just its motion. Both halves are needed for the claim this path makes: the union of
    the inflated samples then contains the continuous padded sweep for every step size,
    so a ``SAFE`` from it is a proof about the sweep as well, and about every finer
    grid. Sampling only the motion would have made the discretised answer *less*
    conservative than the sweep it is supposed to over-approximate, which is the one
    direction this package is not allowed to go. With ``inflate=False`` it is the naive
    per-frame check - discs at sampled instants with nothing between them - and it is
    the thing that lets a fast aircraft jump through another one. Both are built here,
    by the same code, and the difference between them is a metric rather than an argument.
    """
    if step_us <= 0:
        raise SweepError(f"a sample step must be positive, got {step_us}")
    reach = policy.lateral_reach_mm(record.body_radius_mm)
    vertical_half = policy.vertical_reach_mm(record.body_half_height_mm)
    pad = policy.leg_pad_us() if inflate else 0
    pieces: list[PositionPiece] = []
    for index in range(len(track) - 1):
        first, second = track[index], track[index + 1]
        duration = second.at_us - first.at_us
        span_start = first.at_us - pad
        span_end = second.at_us + pad
        samples = -((-(span_end - span_start)) // step_us)
        slack = 0
        vertical_slack = 0
        if inflate:
            travel = second.east_mm - first.east_mm
            drift = second.north_mm - first.north_mm
            climb = second.altitude_mm - first.altitude_mm
            span = max(1, duration)
            slack = int(math.ceil(math.hypot(travel, drift) * step_us / span)) + 1
            vertical_slack = int(math.ceil(abs(climb) * step_us / span)) + 1
        for sample in range(samples + 1):
            at_us = min(span_end, span_start + sample * step_us)
            # Clamped interpolation: outside the motion window the body stays at the
            # leg end, which is the same law the continuous sweep uses for its holds.
            ratio = Fraction(min(max(at_us, first.at_us), second.at_us) - first.at_us,
                             max(1, duration)) if duration else Fraction(0)
            east = int(math.floor(Fraction(first.east_mm) + ratio * (second.east_mm - first.east_mm)))
            north = int(math.floor(Fraction(first.north_mm) + ratio * (second.north_mm - first.north_mm)))
            alt = int(math.floor(Fraction(first.altitude_mm) + ratio * (second.altitude_mm - first.altitude_mm)))
            # An inflated sample claims the interval it stands for; a frame sample
            # claims only the instant it was drawn at. That single difference is the
            # gap between a proof and a picture.
            until_us = min(span_end, at_us + step_us) if inflate else at_us
            pieces.append(
                PositionPiece(
                    leg_index=index, sample_index=sample, kind=PieceKind.SAMPLE,
                    start_us=at_us, end_us=until_us,
                    east_start=east, north_start=north, alt_start=alt,
                    east_end=east, north_end=north, alt_end=alt,
                    reach_mm=reach + slack, vertical_half_mm=vertical_half + vertical_slack,
                )
            )
    return tuple(pieces)


def pad_tail_us(policy: CheckerPolicy) -> int:
    """How far past the last plan point a plan's protection still reaches.

    A separate name for the same policy quantity because the two callers ask
    different questions: the sweep pads every leg, and the pair-level window is
    padded once at each end of the whole plan.
    """
    return int(policy.leg_pad_us())


def sweep_plan(
    record: CommittedPlanRecord,
    frame: PairFrame,
    policy: CheckerPolicy,
    *,
    basis_step_us: int | None = None,
    inflate: bool = True,
) -> SweptPlan:
    """Build the swept plan for one record, on the pair's shared frame.

    ``basis_step_us=None`` asks for the exact continuous sweep. A step size asks for
    the sampled sweep, inflated (a proof) or not (the frame sample, contrast only).

    Raises:
        SweepError: no points, a repeated instant, no single altitude reference, or
            a point outside the shared projected frame. The caller reports these as
            reasons and answers ``UNPROVEN``; it never answers ``SAFE``.
    """
    if len(record.points) < 1:
        raise SweepError(f"{record.plan_version} has no plan points")
    track = _track(record, frame)
    instants = [point.at_us for point in track]
    if len(set(instants)) != len(instants):
        raise SweepError(f"{record.plan_version} repeats an instant, so interpolation has no law")
    if any(first > second for first, second in zip(instants, instants[1:])):
        raise SweepError(f"{record.plan_version} runs backwards in time")
    reference = _reference(record)
    if reference is not policy.altitude_reference:
        raise SweepError(
            f"{record.plan_version} is written in {reference.value} while the active policy "
            f"{policy.policy_version} is in {policy.altitude_reference.value}"
        )
    if len(track) == 1:
        only = track[0]
        held: tuple[PositionPiece, ...] = (
            PositionPiece(
                leg_index=0, sample_index=0, kind=PieceKind.MOTION,
                start_us=only.at_us - policy.leg_pad_us(), end_us=only.at_us + policy.leg_pad_us(),
                east_start=only.east_mm, north_start=only.north_mm, alt_start=only.altitude_mm,
                east_end=only.east_mm, north_end=only.north_mm, alt_end=only.altitude_mm,
                reach_mm=policy.lateral_reach_mm(record.body_radius_mm),
                vertical_half_mm=policy.vertical_reach_mm(record.body_half_height_mm),
            ),
        )
        pieces = held
    elif basis_step_us is None:
        pieces = _motion_pieces(record, track, policy)
    else:
        pieces = _sampled_pieces(record, track, policy, basis_step_us, inflate=inflate)

    first_instant, last_instant = min(instants), max(instants)
    pad = policy.leg_pad_us()
    return SweptPlan(
        plan_version=record.plan_version,
        reference=reference,
        pieces=pieces,
        track_mm=tuple((point.east_mm, point.north_mm) for point in track),
        corridor_keys=_corridor_keys(record),
        window=Interval.of(first_instant - pad, last_instant + pad + 1),
        policy_key=policy.key(),
    )


def _corridor_keys(record: CommittedPlanRecord) -> tuple[str, ...]:
    try:
        return tuple(f"corridor:{lane}" for lane in record.corridor_identifiers())
    except GeomCoreError as error:
        raise SweepError(str(error)) from error


def grid_step_us(seconds: float) -> int:
    """A sampling interval in seconds to integer microseconds, floored once."""
    step = int(math.floor(seconds * MICROS_PER_SECOND))
    if step <= 0:
        raise SweepError(f"a sampling interval must be at least one microsecond, got {seconds} s")
    return step


__all__ = [
    "PairFrame",
    "PieceKind",
    "PositionPiece",
    "SweptPlan",
    "SweepError",
    "TrackPoint",
    "grid_step_us",
    "pad_tail_us",
    "sweep_plan",
]
