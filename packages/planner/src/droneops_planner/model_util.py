"""Integer arithmetic helpers shared by the planner.

Time is microseconds, energy is watt-hours, ratios are basis points. There is no
float anywhere in this module, so a ranking cannot drift between runs (conventions
section 4) and an energy margin cannot be rounded into feasibility.
"""

from __future__ import annotations

from droneops_corridor_graph import canonical_digest

MICROS_PER_SECOND: int = 1_000_000
MICROS_PER_MINUTE: int = 60 * MICROS_PER_SECOND
MICROS_PER_HOUR: int = 60 * MICROS_PER_MINUTE
BP_PER_WHOLE: int = 10_000
MM_PER_METRE: int = 1_000
M_PER_KILOMETRE: int = 1_000


def ceil_div(numerator: int, denominator: int) -> int:
    """Integer ceiling division.

    Estimates round up: a traversal or an energy draw rounded down is a
    reservation that releases too early or a battery that silently runs dry.
    """
    if denominator <= 0:
        raise ValueError(f"denominator must be positive, got {denominator}")
    return -((-numerator) // denominator)


def apply_bp(value: int, basis_points: int) -> int:
    """Scale an integer by basis points, ceilinged."""
    return ceil_div(value * (BP_PER_WHOLE + basis_points), BP_PER_WHOLE)


def minutes_to_us(minutes: int) -> int:
    return minutes * MICROS_PER_MINUTE


def seconds_to_us(seconds: int) -> int:
    return seconds * MICROS_PER_SECOND


def us_to_seconds(microseconds: int) -> int:
    """Floor: a duration reported in seconds must never overstate what was used."""
    return microseconds // MICROS_PER_SECOND


__all__ = [
    "BP_PER_WHOLE",
    "M_PER_KILOMETRE",
    "MICROS_PER_HOUR",
    "MICROS_PER_MINUTE",
    "MICROS_PER_SECOND",
    "MM_PER_METRE",
    "apply_bp",
    "canonical_digest",
    "ceil_div",
    "minutes_to_us",
    "seconds_to_us",
    "us_to_seconds",
]
