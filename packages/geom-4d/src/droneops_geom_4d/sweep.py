"""Swept protected volumes: a moving body, continuously.

A protected volume is not a box around a waypoint. It is the set of places the
body's *protection* covers over an interval, and for a body that moves in a
straight line at a steady climb that set is a capsule in space and a wedge in
height. This module builds it three ways, and the difference between the three is
the whole safety argument:

``sweep_profile``
    the exact continuous sweep: one prism per leg, carrying the leg's own motion
    law. :mod:`droneops_geom_4d.intersect` can then ask about every instant of a
    shared time window, so a fast aircraft cannot pass through another between
    frames.
``discretised_sweep``
    a conservatively discretised sweep: short stationary prisms whose radius is
    inflated by the distance the body can travel inside the sample interval, plus
    geom-core's declared quantisation tolerance. Their union **contains** the
    continuous volume for every step size, which is what lets the two paths be
    reconciled by test rather than by assertion, and what makes them honest for a
    ledger that needs atomic windows.
``frame_snapshots``
    the naive thing: one disc per sampled instant, nothing between. It exists so
    the failure mode is demonstrable and so a visual layer can draw frames; it is
    never a conflict basis, and everything built here is labelled
    ``FRAME_SAMPLE`` so a caller can see what it is holding.

Every prism is a vertical cylinder in one projected frame: a lateral reach around
the track and a vertical reach around the height. Coordinates are only comparable
inside the same frame, so a prism carries its plane's origin and the intersection
kernel refuses - ``UNPROVEN``, never ``SAFE`` - to compare two prisms projected
from different origins. Margins never appear here as numbers: they come from the
versioned :class:`~droneops_geom_4d.policy.ProtectedVolumePolicy`, and each prism
records the policy key it was built with, so a verdict traces back to the
configuration release that produced it.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Self

from droneops_geom_core import (
    QUANTIZATION_TOLERANCE_MM,
    AltitudeBand,
    AltitudeReference,
    Interval,
    LocalPlane,
    Microsecond,
    Millimetre,
    as_millimetres,
    metres_to_millimetres,
)

from . import shapes
from .errors import SweepGeometryError
from .intersect import (
    IntersectionResult,
    ProtectedSlice,
    SweepBasis,
    centre_at,
    profile_verdict,
)
from .policy import ProtectedVolumePolicy
from .profile import ProfileSegment, RouteProfile, exact_axis


@dataclass(frozen=True)
class ProtectedBody:
    """The body being protected, as a vertical cylinder in millimetres."""

    radius_mm: Millimetre
    half_height_mm: Millimetre
    label: str = "body"

    def __post_init__(self) -> None:
        if self.radius_mm < 0 or self.half_height_mm < 0:
            raise SweepGeometryError(
                f"body extent cannot be negative: radius {self.radius_mm} mm, "
                f"half height {self.half_height_mm} mm"
            )
        if not self.label.strip():
            raise SweepGeometryError("a protected body needs a label for the trace")

    @classmethod
    def of(cls, radius_m: float, half_height_m: float, label: str = "body") -> Self:
        """Boundary constructor from metres, quantised once."""
        return cls(metres_to_millimetres(radius_m), metres_to_millimetres(half_height_m), label)


def protected_radius_mm(body: ProtectedBody, policy: ProtectedVolumePolicy) -> Millimetre:
    """Lateral reach: body radius + separation margin + navigation uncertainty."""
    return Millimetre(body.radius_mm + policy.horizontal_reach_mm)


def protected_vertical_half_mm(body: ProtectedBody, policy: ProtectedVolumePolicy) -> Millimetre:
    """Vertical reach: body half height + vertical margin."""
    return Millimetre(body.half_height_mm + policy.vertical_reach_mm)


@dataclass(frozen=True)
class SweptPrism:
    """One swept protected volume, and the motion law inside it.

    Endpoints are integer millimetres at integer microsecond instants, so the
    centre at any instant is exactly representable as a rational and the contact
    test needs no floating point. ``pad_us`` is how far the volume is widened in
    time: timing uncertainty always, transition buffer additionally on a leg that
    climbs or descends (see :func:`sweep_profile`). ``plane_origin`` is the
    geodetic point the millimetre coordinates hang off; two prisms with different
    origins are not in the same universe and must never be intersected.
    """

    volume_id: str
    basis: SweepBasis
    reference: AltitudeReference
    authoritative: bool
    source: str
    policy_key: str
    plane_origin: tuple[float, float]
    segment_index: int
    motion_start_us: Microsecond
    motion_end_us: Microsecond
    pad_us: Microsecond
    start_east_mm: Millimetre
    start_north_mm: Millimetre
    start_alt_mm: Millimetre
    end_east_mm: Millimetre
    end_north_mm: Millimetre
    end_alt_mm: Millimetre
    radius_mm: Millimetre
    vertical_half_mm: Millimetre

    def __post_init__(self) -> None:
        if self.motion_end_us < self.motion_start_us:
            raise SweepGeometryError(f"{self.volume_id} runs backwards in time")
        if self.pad_us < 0:
            raise SweepGeometryError(f"{self.volume_id} has a negative time pad")
        if self.radius_mm < 0 or self.vertical_half_mm < 0:
            raise SweepGeometryError(f"{self.volume_id} has a negative extent")

    def window(self) -> Interval:
        """Half-open interval holding every instant this volume is alive on."""
        return Interval.of(self.motion_start_us - self.pad_us, self.motion_end_us + self.pad_us + 1)

    def band(self) -> AltitudeBand:
        """Conservative vertical extent over the whole window, closed at both ends."""
        heights = (
            self.start_alt_mm - self.vertical_half_mm,
            self.end_alt_mm - self.vertical_half_mm,
            self.start_alt_mm + self.vertical_half_mm,
            self.end_alt_mm + self.vertical_half_mm,
        )
        return AltitudeBand(
            self.reference, as_millimetres(min(heights)), as_millimetres(max(heights) + 1)
        )

    def centre_at(self, at_us: int) -> tuple[Fraction, Fraction, Fraction]:
        """Exact ``(east, north, altitude)`` of the body centre at an instant."""
        return centre_at(self, at_us)

    def instantaneous_slice(self, at_us: int) -> ProtectedSlice:
        """The exact 3D protected volume at one instant of this sweep.

        This is the containment oracle: the discretised path is required to cover
        the continuous path, and it is checked by asking exactly this question at
        instants where the continuous answer is known.
        """
        east, north, altitude = self.centre_at(at_us)
        return ProtectedSlice(
            at_us=Microsecond(at_us),
            east_mm=as_millimetres(math.floor(east)),
            north_mm=as_millimetres(math.floor(north)),
            altitude_mm=as_millimetres(math.floor(altitude)),
            radius_mm=self.radius_mm,
            vertical_half_mm=self.vertical_half_mm,
            reference=self.reference,
        )

    def footprint_wkt(self) -> str:
        """Horizontal footprint: the capsule this body sweeps, buffered by its reach."""
        return shapes.capsule_wkt(
            [(self.start_east_mm, self.start_north_mm), (self.end_east_mm, self.end_north_mm)],
            self.radius_mm,
        )

    def contains(self, east_mm: int, north_mm: int, altitude_mm: int, at_us: int) -> bool:
        """Is this place, at this instant, inside the protected volume?"""
        return self.instantaneous_slice(at_us).contains(east_mm, north_mm, altitude_mm)

    def is_moving(self) -> bool:
        """Whether the body's centre changes place inside this volume."""
        return (self.start_east_mm, self.start_north_mm, self.start_alt_mm) != (
            self.end_east_mm,
            self.end_north_mm,
            self.end_alt_mm,
        )


def _prism(
    *,
    source: str,
    basis: SweepBasis,
    plane_origin: tuple[float, float],
    segment_index: int,
    sample_index: int,
    authoritative: bool,
    policy: ProtectedVolumePolicy,
    motion_start_us: int,
    motion_end_us: int,
    pad_us: int,
    start: tuple[int, int, int],
    end: tuple[int, int, int],
    radius_mm: int,
    vertical_half_mm: int,
) -> SweptPrism:
    """One construction site for every basis, so ids and keys stay consistent."""
    return SweptPrism(
        volume_id=f"{source}:{basis.value.lower()}:{segment_index}:{sample_index}",
        basis=basis,
        reference=policy.altitude_reference,
        authoritative=authoritative,
        source=source,
        policy_key=policy.key(),
        plane_origin=plane_origin,
        segment_index=segment_index,
        motion_start_us=Microsecond(motion_start_us),
        motion_end_us=Microsecond(motion_end_us),
        pad_us=Microsecond(pad_us),
        start_east_mm=as_millimetres(start[0]),
        start_north_mm=as_millimetres(start[1]),
        start_alt_mm=as_millimetres(start[2]),
        end_east_mm=as_millimetres(end[0]),
        end_north_mm=as_millimetres(end[1]),
        end_alt_mm=as_millimetres(end[2]),
        radius_mm=as_millimetres(radius_mm),
        vertical_half_mm=as_millimetres(vertical_half_mm),
    )


def _plane_origin(plane: LocalPlane) -> tuple[float, float]:
    """The projected frame's identity, as the canonical sort key geom-core gives it."""
    return plane.reference.sort_key()


def _require_reference(profile: RouteProfile, policy: ProtectedVolumePolicy) -> None:
    """A policy in one altitude frame cannot inflate a plan written in another.

    Refusing here is deliberate: converting between ``AGL`` and ``AMSL`` needs
    site datums, and guessing them would turn an unprovable question into a
    confident answer.
    """
    if profile.reference is not policy.altitude_reference:
        raise SweepGeometryError(
            f"profile {profile.plan_version} is in {profile.reference.value} but policy "
            f"{policy.policy_version} is in {policy.altitude_reference.value}; supply "
            "geom_core.VerticalDatums and restate one of them"
        )


def sweep_profile(
    profile: RouteProfile, body: ProtectedBody, policy: ProtectedVolumePolicy
) -> tuple[SweptPrism, ...]:
    """The exact continuous sweep: one swept prism per leg, in leg order.

    A leg that changes height also carries the policy's transition buffer in
    time, because a climb or descent is a manoeuvre, not a position: the
    protected volume has to survive the pilot or the controller being late.
    """
    _require_reference(profile, policy)
    radius = protected_radius_mm(body, policy)
    vertical_half = protected_vertical_half_mm(body, policy)
    origin = _plane_origin(profile.plane)
    prisms: list[SweptPrism] = []
    for segment in profile.segments():
        pad: int = policy.timing_uncertainty_us
        if segment.is_vertical_change:
            pad += policy.transition_buffer_us
        prisms.append(
            _prism(
                source=profile.source,
                basis=SweepBasis.CONTINUOUS,
                plane_origin=origin,
                segment_index=segment.index,
                sample_index=0,
                authoritative=profile.authoritative,
                policy=policy,
                motion_start_us=segment.start.at_us,
                motion_end_us=segment.end.at_us,
                pad_us=pad,
                start=(
                    segment.start_offset.east_mm,
                    segment.start_offset.north_mm,
                    segment.start.altitude.value_mm,
                ),
                end=(
                    segment.end_offset.east_mm,
                    segment.end_offset.north_mm,
                    segment.end.altitude.value_mm,
                ),
                radius_mm=radius,
                vertical_half_mm=vertical_half,
            )
        )
    return tuple(prisms)


def _inflation_mm(rate: Fraction, span_us: int) -> Millimetre:
    """How far a body can move in ``span_us`` at ``rate``, rounded outward.

    The quantisation tolerance is added because the sampled centre is itself a
    floored integer: without it a discretised volume could be a hair smaller than
    the continuous one it is supposed to contain.
    """
    travel = -((-abs(rate) * span_us) // 1)
    return Millimetre(travel + QUANTIZATION_TOLERANCE_MM)


def quantised(segment: ProfileSegment, at_us: int) -> tuple[int, int, int]:
    """Exact ``(east, north, altitude)`` at an instant, floored to whole units."""
    return (
        math.floor(
            exact_axis(
                at_us,
                segment.start.at_us,
                segment.end.at_us,
                segment.start_offset.east_mm,
                segment.end_offset.east_mm,
            )
        ),
        math.floor(
            exact_axis(
                at_us,
                segment.start.at_us,
                segment.end.at_us,
                segment.start_offset.north_mm,
                segment.end_offset.north_mm,
            )
        ),
        math.floor(
            exact_axis(
                at_us,
                segment.start.at_us,
                segment.end.at_us,
                segment.start.altitude.value_mm,
                segment.end.altitude.value_mm,
            )
        ),
    )


def discretised_sweep(
    profile: RouteProfile,
    body: ProtectedBody,
    policy: ProtectedVolumePolicy,
    step_us: int,
) -> tuple[SweptPrism, ...]:
    """Conservative discretisation: stationary prisms that contain the sweep.

    For a sample interval of length ``d`` on a leg, the true centre is never
    further than ``speed * d`` from the sampled instant and the true height never
    further than ``|vertical rate| * d``. Both are added to the protected extent,
    so the union of these prisms covers the continuous volume **for every step
    size** - smaller steps only make it tighter. That containment is what the
    property tests assert, and what makes "the discretised path agrees" a theorem
    rather than a coincidence.
    """
    if step_us <= 0:
        raise SweepGeometryError(f"discretisation step must be positive, got {step_us}")
    _require_reference(profile, policy)
    radius = protected_radius_mm(body, policy)
    vertical_half = protected_vertical_half_mm(body, policy)
    origin = _plane_origin(profile.plane)
    prisms: list[SweptPrism] = []
    sample_index = 0
    for segment in profile.segments():
        chunks = -((-segment.duration_us) // step_us)
        speed = segment.speed_mm_per_us
        vertical_rate = abs(segment.vertical_rate_mm_per_us)
        for chunk in range(chunks):
            start_us = segment.start.at_us + chunk * step_us
            stop_us = min(segment.end.at_us, start_us + step_us)
            interval = stop_us - start_us
            centre = quantised(segment, start_us)
            pad = policy.timing_uncertainty_us + interval
            if segment.is_vertical_change:
                pad += policy.transition_buffer_us
            prisms.append(
                _prism(
                    source=profile.source,
                    basis=SweepBasis.DISCRETISED,
                    plane_origin=origin,
                    segment_index=segment.index,
                    sample_index=sample_index,
                    authoritative=profile.authoritative,
                    policy=policy,
                    motion_start_us=start_us,
                    motion_end_us=start_us,
                    pad_us=pad,
                    start=centre,
                    end=centre,
                    radius_mm=radius + _inflation_mm(speed, interval),
                    vertical_half_mm=vertical_half + _inflation_mm(vertical_rate, interval),
                )
            )
            sample_index += 1
    return tuple(prisms)


def _frame_instants(start_us: int, end_us: int, step_us: int) -> tuple[int, ...]:
    """Sampled instants from the first to the last, inclusive, on a fixed grid."""
    count = -((-(end_us - start_us)) // step_us) + 1
    return tuple(min(end_us, start_us + index * step_us) for index in range(count))


def frame_snapshots(
    profile: RouteProfile,
    body: ProtectedBody,
    policy: ProtectedVolumePolicy,
    step_us: int,
) -> tuple[SweptPrism, ...]:
    """Protected discs at sampled instants, with nothing in between.

    This is the representation that lets a fast aircraft jump through another
    one. It exists so the failure is testable and so a visual layer can draw
    frames; it is never a conflict basis.
    """
    if step_us <= 0:
        raise SweepGeometryError(f"frame step must be positive, got {step_us}")
    _require_reference(profile, policy)
    radius = protected_radius_mm(body, policy)
    vertical_half = protected_vertical_half_mm(body, policy)
    origin = _plane_origin(profile.plane)
    window = profile.window
    prisms: list[SweptPrism] = []
    for index, at_us in enumerate(_frame_instants(window.start, window.end, step_us)):
        segment = profile.segment_at(at_us)
        centre = quantised(segment, at_us)
        prisms.append(
            _prism(
                source=profile.source,
                basis=SweepBasis.FRAME_SAMPLE,
                plane_origin=origin,
                segment_index=segment.index,
                sample_index=index,
                authoritative=profile.authoritative,
                policy=policy,
                motion_start_us=at_us,
                motion_end_us=at_us,
                pad_us=0,
                start=centre,
                end=centre,
                radius_mm=radius,
                vertical_half_mm=vertical_half,
            )
        )
    return tuple(prisms)


def static_volume(
    *,
    policy: ProtectedVolumePolicy,
    body: ProtectedBody,
    plane: LocalPlane,
    source: str,
    plan_version: str,
    east_mm: int,
    north_mm: int,
    altitude_mm: int,
    reference: AltitudeReference,
    from_us: int,
    to_us: int,
    authoritative: bool = False,
) -> SweptPrism:
    """A stationary protected volume: an airborne obstruction, a pad, a hover.

    Anything that occupies a place for a while is still a 4D volume, and an
    aircraft planned through it must be caught by the same code path as an
    aircraft planned through another aircraft. ``plane`` says which projected
    frame its millimetres belong to, so an obstruction can be placed on the same
    frame as the routes that have to avoid it.
    """
    if to_us < from_us:
        raise SweepGeometryError(f"static volume {plan_version} runs backwards in time")
    if reference is not policy.altitude_reference:
        raise SweepGeometryError(
            f"static volume {plan_version} is in {reference.value} while policy "
            f"{policy.policy_version} is in {policy.altitude_reference.value}"
        )
    centre = (as_millimetres(east_mm), as_millimetres(north_mm), as_millimetres(altitude_mm))
    return _prism(
        source=f"{source}@{plan_version}",
        basis=SweepBasis.CONTINUOUS,
        plane_origin=_plane_origin(plane),
        segment_index=0,
        sample_index=0,
        authoritative=authoritative,
        policy=policy,
        motion_start_us=from_us,
        motion_end_us=to_us,
        pad_us=policy.timing_uncertainty_us,
        start=centre,
        end=centre,
        radius_mm=protected_radius_mm(body, policy),
        vertical_half_mm=protected_vertical_half_mm(body, policy),
    )


def volumes_in_order(volumes: Sequence[SweptPrism]) -> tuple[SweptPrism, ...]:
    """Canonical sweep order: leg, then identifier.

    Explicit because a verdict must not depend on the order two lists happened to
    arrive in, and because iterating an unordered collection to produce output is
    forbidden (conventions section 4).
    """
    return tuple(sorted(volumes, key=lambda item: (item.segment_index, item.volume_id)))


def conflict_verdict(
    left: RouteProfile,
    right: RouteProfile,
    body: ProtectedBody,
    policy: ProtectedVolumePolicy,
) -> IntersectionResult:
    """Exact continuous profile-versus-profile verdict over all of time.

    The headline call: two plans, one answer, no step size to argue about because
    nothing was sampled.
    """
    return profile_verdict(sweep_profile(left, body, policy), sweep_profile(right, body, policy))


def discretised_conflict_verdict(
    left: RouteProfile,
    right: RouteProfile,
    body: ProtectedBody,
    policy: ProtectedVolumePolicy,
    step_us: int,
) -> IntersectionResult:
    """The same question asked of the conservatively discretised sweep.

    Kept beside :func:`conflict_verdict` rather than behind it so a caller can see
    which representation produced the answer, and so a test can demand that the
    two agree in the direction that matters: a discretised ``SAFE`` is a proof,
    while a discretised ``CONFLICT`` may be the inflation talking.
    """
    return profile_verdict(
        discretised_sweep(left, body, policy, step_us),
        discretised_sweep(right, body, policy, step_us),
    )


def frame_conflict_verdict(
    left: RouteProfile,
    right: RouteProfile,
    body: ProtectedBody,
    policy: ProtectedVolumePolicy,
    step_us: int,
) -> IntersectionResult:
    """The per-frame answer, which is why per-frame answers are banned.

    Returned for contrast in tests and metrics. Anything this reports as ``SAFE``
    while :func:`conflict_verdict` reports ``CONFLICT`` is the aircraft that jumped
    through another one between frames.
    """
    return profile_verdict(
        frame_snapshots(left, body, policy, step_us),
        frame_snapshots(right, body, policy, step_us),
    )
