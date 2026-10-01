"""Climb and descent envelopes, and the layers a manoeuvre passes through.

A leg that changes height is not a translation of a level leg. Two things about
it need their own volume, and both come from a policy input rather than a
guess:

**Transition buffer.** While an aircraft is climbing or descending, it is in a
manoeuvre, and a manoeuvre is exactly the part of a plan that a controller or an
onboard failure makes late. The policy's ``transition_buffer_s`` is therefore
applied twice over: the manoeuvre's time window is widened by it, and its
footprint is extended by the distance the body can still travel at its own speed
during that buffer. A volume that widened only in time would claim lateral
knowledge it does not have.

**Layer occupancy.** A climbing body is inside every altitude layer between its
start and its end, and it is inside them *at specific times*. A reservation
ledger and a checker both need that per-layer view, because the resource being
contended is a corridor layer, not a point. The layer lattice is the caller's
grid: pass ``GridSpec.vertical_band_mm`` and the bands are exactly the ones the
rest of the system keys on.

The altitude-to-time inversion is done with rationals and rounded outward on
both ends, so a reported window is never narrower than the truth.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from fractions import Fraction

from droneops_geom_core import (
    AltitudeBand,
    AltitudeReference,
    Interval,
    Microsecond,
    Millimetre,
    as_millimetres,
    floor_div,
)

from . import shapes
from .errors import SweepGeometryError
from .policy import ProtectedVolumePolicy
from .profile import ProfileSegment, RouteProfile
from .sweep import ProtectedBody, protected_radius_mm, protected_vertical_half_mm


class Manoeuvre(StrEnum):
    CLIMB = "CLIMB"
    DESCENT = "DESCENT"

    @classmethod
    def for_leg(cls, start_altitude_mm: int, end_altitude_mm: int) -> Manoeuvre:
        """Direction of a leg. A level leg has no manoeuvre and is not asked for one."""
        if end_altitude_mm == start_altitude_mm:
            raise SweepGeometryError("a level leg is not a manoeuvre")
        return cls.CLIMB if end_altitude_mm > start_altitude_mm else cls.DESCENT


@dataclass(frozen=True)
class TransitionEnvelope:
    """The protected volume of one manoeuvre, buffer included."""

    envelope_id: str
    manoeuvre: Manoeuvre
    segment_index: int
    window: Interval
    footprint_wkt: str
    band: AltitudeBand
    transition_buffer_us: Microsecond
    along_track_extension_mm: Millimetre
    reference: AltitudeReference
    policy_key: str
    authoritative: bool


@dataclass(frozen=True)
class LayerOccupancy:
    """Where and when a manoeuvring body sits inside one altitude layer."""

    occupancy_id: str
    manoeuvre: Manoeuvre
    segment_index: int
    layer: AltitudeBand
    window: Interval
    footprint_wkt: str
    policy_key: str
    authoritative: bool

    @property
    def layer_index(self) -> int:
        """Band index on the lattice the caller supplied."""
        return floor_div(self.layer.lower_mm, self.layer.measure)


def _leg_radius(body: ProtectedBody, policy: ProtectedVolumePolicy) -> int:
    return protected_radius_mm(body, policy)


def _leg_band(segment: ProfileSegment, vertical_half_mm: int) -> AltitudeBand:
    low, high = segment.altitude_range_mm
    return AltitudeBand(
        segment.start.altitude.reference,
        as_millimetres(low - vertical_half_mm),
        as_millimetres(high + vertical_half_mm + 1),
    )


def _altitude_time_range(
    segment: ProfileSegment, low_mm: int, high_mm: int
) -> tuple[int, int] | None:
    """Integer instants at which the body's centre is inside ``[low_mm, high_mm]``.

    Rounded outward: the returned span is a superset of the true occupancy, which
    is the only direction an uncertainty window is allowed to err in.
    """
    start_alt = segment.start.altitude.value_mm
    end_alt = segment.end.altitude.value_mm
    duration = segment.duration_us
    if end_alt == start_alt:
        return (segment.start.at_us, segment.end.at_us) if low_mm <= start_alt <= high_mm else None
    first = Fraction((low_mm - start_alt) * duration, end_alt - start_alt)
    second = Fraction((high_mm - start_alt) * duration, end_alt - start_alt)
    lower, upper = (first, second) if first <= second else (second, first)
    lo = max(segment.start.at_us, math.floor(segment.start.at_us + lower))
    hi = min(segment.end.at_us, math.ceil(segment.start.at_us + upper))
    return (lo, hi) if lo <= hi else None


def climb_descent_envelopes(
    profile: RouteProfile, body: ProtectedBody, policy: ProtectedVolumePolicy
) -> tuple[TransitionEnvelope, ...]:
    """One envelope per climbing or descending leg, in leg order.

    Level legs produce nothing: their protected volume is the swept prism, and
    inventing a second volume for them would double-count the same airspace.
    """
    radius = _leg_radius(body, policy)
    vertical_half = protected_vertical_half_mm(body, policy)
    buffer_us = policy.transition_buffer_us
    envelopes: list[TransitionEnvelope] = []
    for segment in profile.segments():
        if not segment.is_vertical_change:
            continue
        manoeuvre = Manoeuvre.for_leg(
            segment.start.altitude.value_mm, segment.end.altitude.value_mm
        )
        extension = policy.along_track_mm(segment.speed_mm_per_us, buffer_us)
        pad = policy.leg_pad_us(True)  # every envelope here is a manoeuvre leg
        envelopes.append(
            TransitionEnvelope(
                envelope_id=f"{profile.source}:envelope:{segment.index}:{manoeuvre.value.lower()}",
                manoeuvre=manoeuvre,
                segment_index=segment.index,
                window=Interval.of(segment.start.at_us - pad, segment.end.at_us + pad + 1),
                footprint_wkt=shapes.capsule_wkt(
                    [
                        (segment.start_offset.east_mm, segment.start_offset.north_mm),
                        (segment.end_offset.east_mm, segment.end_offset.north_mm),
                    ],
                    radius + extension,
                ),
                band=_leg_band(segment, vertical_half),
                transition_buffer_us=Microsecond(buffer_us),
                along_track_extension_mm=extension,
                reference=profile.reference,
                policy_key=policy.key(),
                authoritative=profile.authoritative,
            )
        )
    return tuple(envelopes)


def layer_occupancies(
    profile: RouteProfile,
    body: ProtectedBody,
    policy: ProtectedVolumePolicy,
    vertical_band_mm: int,
) -> tuple[LayerOccupancy, ...]:
    """Per-layer occupancy of every manoeuvre, ordered by leg then layer.

    ``vertical_band_mm`` is the caller's grid lattice (``GridSpec.vertical_band_mm``),
    so these bands are the same bands the corridor and the ledger key on - not a
    second, slightly different vertical decomposition invented here.
    """
    if vertical_band_mm <= 0:
        raise SweepGeometryError(f"vertical band height must be positive, got {vertical_band_mm}")
    vertical_half = protected_vertical_half_mm(body, policy)
    radius = _leg_radius(body, policy)
    buffer_us = policy.transition_buffer_us
    pad = policy.timing_uncertainty_us + buffer_us
    occupancies: list[LayerOccupancy] = []
    for segment in profile.segments():
        if not segment.is_vertical_change:
            continue
        manoeuvre = Manoeuvre.for_leg(
            segment.start.altitude.value_mm, segment.end.altitude.value_mm
        )
        band = _leg_band(segment, vertical_half)
        first = floor_div(band.lower_mm, vertical_band_mm)
        last = floor_div(band.upper_mm - 1, vertical_band_mm)
        extension = policy.along_track_mm(segment.speed_mm_per_us, buffer_us)
        footprint = shapes.capsule_wkt(
            [
                (segment.start_offset.east_mm, segment.start_offset.north_mm),
                (segment.end_offset.east_mm, segment.end_offset.north_mm),
            ],
            radius + extension,
        )
        for index in range(first, last + 1):
            lower = index * vertical_band_mm
            upper = (index + 1) * vertical_band_mm - 1
            # The body's protected extent, not just its centre, is what touches a layer.
            span = _altitude_time_range(
                segment, lower - vertical_half - 1, upper + vertical_half + 1
            )
            if span is None:
                continue
            lo, hi = span
            occupancies.append(
                LayerOccupancy(
                    occupancy_id=f"{profile.source}:layer:{segment.index}:{index}",
                    manoeuvre=manoeuvre,
                    segment_index=segment.index,
                    layer=AltitudeBand(
                        profile.reference, as_millimetres(lower), as_millimetres(upper + 1)
                    ),
                    window=Interval.of(lo - pad, hi + pad + 1),
                    footprint_wkt=footprint,
                    policy_key=policy.key(),
                    authoritative=profile.authoritative,
                )
            )
    return tuple(occupancies)


def describe_envelope(envelope: TransitionEnvelope) -> str:
    """One-line, deterministic summary for a decision trace."""
    return (
        f"{envelope.envelope_id} {envelope.manoeuvre.value} "
        f"t={envelope.window.start}..{envelope.window.end} "
        f"band={envelope.band.lower_mm}..{envelope.band.upper_mm}mm "
        f"buffer={envelope.transition_buffer_us}us "
        f"along_track={envelope.along_track_extension_mm}mm"
    )
