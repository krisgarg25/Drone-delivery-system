"""Separate, seeded random streams (docs/conventions.md section 4).

One stream per noise source, derived from the run seed with fixed prime offsets so the
streams cannot overlap and so a change to one source's draw count cannot shift another.
`random.Random` instances are process-independent: PYTHONHASHSEED has no effect.
"""

from __future__ import annotations

import random

STREAM_OFFSETS: dict[str, int] = {
    "demand": 0,
    "wind": 1_000_003,
    "battery": 2_000_069,
    "faults": 3_000_041,
}

STREAM_NAMES = tuple(sorted(STREAM_OFFSETS))


class DeterministicStreams:
    """Four independent PRNG streams; never a fresh global `random` call."""

    def __init__(self, seed: int) -> None:
        self._rngs: dict[str, random.Random] = {
            name: random.Random(seed + STREAM_OFFSETS[name]) for name in STREAM_NAMES
        }

    def demand_jitter_us(self, span_us: int) -> int:
        if span_us <= 0:
            return 0
        return self._rngs["demand"].randrange(0, span_us)

    def wind_delta_ms(self, bound_ms: int) -> int:
        if bound_ms <= 0:
            return 0
        return self._rngs["wind"].randint(-bound_ms, bound_ms)

    def battery_factor_bp(self, spread_bp: int) -> int:
        """Multiplicative energy noise as basis points around 10_000."""
        return 10_000 + self._rngs["battery"].randint(-spread_bp, spread_bp)

    def fault_draw(self) -> int:
        """Uniform per-ten-thousand draw for stochastic fault injection."""
        return self._rngs["faults"].randrange(0, 10_000)
