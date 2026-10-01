"""Integer arithmetic primitives for the energy kernel.

conventions.md section 10 requires decimal-or-integer arithmetic for anything
energy-critical, so every quantity in this package is an integer: millimetres,
microseconds, deci-metres-per-second, milli-watt-hours and basis points. The
kernel has no float arithmetic path at all except the two trigonometric
components per leg (see :mod:`droneops_energy.model`), which are immediately
quantised to basis points so nothing downstream can be float-sensitive.

Rounding direction is a safety decision, not a style choice: consumption and
power are rounded **up**, available energy is rounded **down**, so a reported
margin is never optimistic.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise

from .errors import EnergyInputError

#: Basis points in one whole unit: 10_000 bp is exactly 100 %.
BP_PER_UNIT = 10_000

#: Milliwatt-hours in one watt-hour. Energy is carried at milliwatt resolution.
MWH_PER_WH = 1_000

#: Microseconds in one second.
US_PER_S = 1_000_000

#: Microseconds-watt a watt-hour equals: 1 W held for 3_600_000_000 us is 1 Wh.
US_W_PER_WH = 3_600_000_000

#: Millimetres in one deci-metre: converts deci-metres/second to millimetres/second.
MM_PER_DM = 100

#: Grams in one kilogram.
G_PER_KG = 1_000


def ceil_div(numerator: int, denominator: int) -> int:
    """Divide, rounding *up* for any sign combination (energy consumption)."""
    if denominator == 0:
        raise EnergyInputError("division by zero in an energy term")
    if denominator < 0:
        return ceil_div(-numerator, -denominator)
    return -((-numerator) // denominator)


def apply_bp(value: int, bp: int, *, round_up: bool) -> int:
    """Scale ``value`` by a basis-point factor, rounding in the safe direction."""
    product = value * bp
    return ceil_div(product, BP_PER_UNIT) if round_up else product // BP_PER_UNIT


def watt_us_to_mwh(power_w: int, time_us: int) -> int:
    """Energy of a constant power over a microsecond span, in milliwatt-hours.

    ``W x us`` is a joule scaled by a thousand; dividing by ``US_W_PER_WH`` turns
    it into milliwatt-hours, rounded up so a partial milliwatt-hour of burn is
    never dropped.
    """
    return ceil_div(power_w * time_us * MWH_PER_WH, US_W_PER_WH)


def mwh_to_wh(value_mwh: int) -> int:
    """Milliwatt-hours to watt-hours, rounded up: never under-report consumption."""
    return ceil_div(value_mwh, MWH_PER_WH)


@dataclass(frozen=True)
class CurvePoint:
    """One point of a versioned characteristic curve: ``x`` measured, ``y`` in bp."""

    x: int
    y: int

    def canonical(self) -> str:
        return f"{self.x}:{self.y}"


def parse_curve(text: str) -> tuple[CurvePoint, ...]:
    """Parse a stored curve, ``"x:y,x:y,..."``, back into points."""
    points: list[CurvePoint] = []
    for row in text.split(","):
        parts = row.strip().split(":")
        if len(parts) != 2:
            raise EnergyInputError(f"curve point {row!r} is not 'x:y'")
        try:
            points.append(CurvePoint(int(parts[0]), int(parts[1])))
        except ValueError as error:  # pragma: no cover - message carries the row
            raise EnergyInputError(f"curve point {row!r} is not integer") from error
    return validate_curve(points)


def format_curve(points: Sequence[CurvePoint]) -> str:
    """Render a curve back to the canonical stored string form."""
    return ",".join(point.canonical() for point in points)


def validate_curve(points: Sequence[CurvePoint]) -> tuple[CurvePoint, ...]:
    """Curves must be non-empty with strictly ascending x, so interpolation is total."""
    ordered = tuple(points)
    if not ordered:
        raise EnergyInputError("a characteristic curve needs at least one point")
    for left, right in pairwise(ordered):
        if left.x >= right.x:
            raise EnergyInputError(f"curve x must strictly increase, found {left.x} then {right.x}")
    return ordered


def interpolate_curve(
    points: Sequence[CurvePoint], x: int, *, round_up: bool, name: str = "curve"
) -> int:
    """Piecewise-linear interpolation, entirely in integers, clamped at both ends.

    Outside the measured range the nearest endpoint is held: extrapolating a
    battery curve past its tabulated points would invent a number nobody supplied.
    """
    ordered = points
    if not ordered:
        raise EnergyInputError(f"{name}: a characteristic curve needs at least one point")
    first = ordered[0]
    if x <= first.x:
        return first.y
    last = ordered[-1]
    if x >= last.x:
        return last.y
    for left, right in pairwise(ordered):
        if x > right.x:
            continue
        span = right.x - left.x
        numerator = left.y * span + (x - left.x) * (right.y - left.y)
        return ceil_div(numerator, span) if round_up else numerator // span
    raise EnergyInputError(f"{name}: interpolation fell through a sorted curve at x={x}")


def require(condition: bool, message: str) -> None:
    """Assert a domain invariant with a reviewable message (never ``assert``)."""
    if not condition:
        raise EnergyInputError(message)
