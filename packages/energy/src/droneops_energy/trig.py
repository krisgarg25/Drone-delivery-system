"""Trigonometry boundary: the kernel's only float use, and it is quantised.

A wind triangle needs sine and cosine. ``math.sin``/``math.cos`` return IEEE
doubles, so this module converts them *immediately* into integer basis points and
nothing downstream of it ever sees a float. Documented tolerance: each wind
component is quantised to 5e-5 of the wind vector (half a basis point), so a leg's
ground speed is uncertain by under 0.05 mm/s and its energy by well under one
milliwatt-hour - three orders of magnitude below the watt-hour resolution the
model reports, and far below the illustrative uncertainty of the coefficients
themselves.
"""

from __future__ import annotations

import math

from .numeric import BP_PER_UNIT, require

#: Millidegrees in a full turn. Bearings are integers, never float degrees.
MDEG_PER_TURN = 360_000

#: Half a turn in millidegrees.
MDEG_PER_HALF_TURN = 180_000


def normalize_mdeg(angle_mdeg: int) -> int:
    """Fold any bearing into ``[0, 360_000)`` millidegrees."""
    return angle_mdeg % MDEG_PER_TURN


def signed_delta_mdeg(from_mdeg: int, to_mdeg: int) -> int:
    """Smallest signed turn from one bearing to the other, in millidegrees."""
    delta = normalize_mdeg(to_mdeg) - normalize_mdeg(from_mdeg)
    if delta > MDEG_PER_HALF_TURN:
        delta -= MDEG_PER_TURN
    elif delta <= -MDEG_PER_HALF_TURN:
        delta += MDEG_PER_TURN
    return delta


def _round_to_int(value: float) -> int:
    """Deterministic half-up rounding of a double to an integer."""
    return math.floor(value + 0.5)


def sin_bp(angle_mdeg: int) -> int:
    """Sine of an angle in millidegrees, returned in basis points."""
    radians = math.radians(signed_delta_mdeg(0, angle_mdeg) / 1000)
    return _round_to_int(math.sin(radians) * BP_PER_UNIT)


def cos_bp(angle_mdeg: int) -> int:
    """Cosine of an angle in millidegrees, returned in basis points."""
    radians = math.radians(signed_delta_mdeg(0, angle_mdeg) / 1000)
    return _round_to_int(math.cos(radians) * BP_PER_UNIT)


def wind_components_bp(wind_course_mdeg: int, track_mdeg: int) -> tuple[int, int]:
    """Decompose a wind vector's *direction* into ``(along_bp, cross_bp)``.

    ``along_bp`` is positive for a tailwind and negative for a headwind;
    ``cross_bp`` is positive for wind pushing the aircraft to the right of track.
    Both are scaled by the wind speed by the caller, in integers.
    """
    delta = signed_delta_mdeg(track_mdeg, wind_course_mdeg)
    along = cos_bp(delta)
    cross = sin_bp(delta)
    require(abs(cross) <= BP_PER_UNIT, "trigonometric component escaped its range")
    return along, cross
