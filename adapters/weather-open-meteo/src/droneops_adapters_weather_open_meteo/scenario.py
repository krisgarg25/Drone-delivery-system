"""Replay the scenario's frozen wind trace as a ``WindSnapshot``.

``simulation/scenarios/chandigarh-synthetic-v1/weather.yaml`` stores, per minute,
a sustained speed in metres per second and a ``headwind_bp`` fraction: the share of
that wind that opposes the scenario's reference course. That is enough to rebuild a
wind vector, and this module does it deterministically - the file is read by the
caller and passed in as already-parsed rows, so the adapter holds no file handle and
no clock either.

The vector is reconstructed as: along-track component = ``-speed x headwind_bp``,
cross-track component = the remainder by Pythagoras, sign resolved to the right of
the reference course. The choice of side is arbitrary but fixed, so two runs get the
same answer; a run that wants the mirror image passes a mirrored course.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence

from droneops_energy import WindSnapshot, normalize_mdeg, snapshot_from_rows
from droneops_energy.errors import EnergyInputError

#: Basis points in one whole unit, and the millidegrees in a half turn.
BP_PER_UNIT = 10_000
MDEG_PER_TURN = 360_000


class TraceError(EnergyInputError):
    """A scenario weather row is missing a field or contradicts its own units."""


def _field(row: Mapping[str, object], key: str, index: int) -> int:
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise TraceError(f"scenario weather row {index} has no integer {key!r}: {value!r}")
    return value


def _vector(speed_dmps: int, headwind_bp: int, course_mdeg: int) -> tuple[int, int]:
    """Reconstruct ``(speed, towards-bearing)`` from a headwind fraction."""
    if speed_dmps < 0:
        raise TraceError("sustained wind cannot be negative")
    fraction = max(-BP_PER_UNIT, min(BP_PER_UNIT, headwind_bp))
    if speed_dmps == 0:
        return 0, course_mdeg
    # Along is negative for a headwind; the cross component takes the remainder,
    # which keeps the reconstructed magnitude equal to the reported speed.
    along = -speed_dmps * fraction // BP_PER_UNIT
    cross_squared = speed_dmps * speed_dmps - along * along
    cross = math.isqrt(max(0, cross_squared))
    bearing = (
        course_mdeg + MDEG_PER_TURN // 2 + (round(math.degrees(math.atan2(cross, along))) * 1000)
    )
    return speed_dmps, normalize_mdeg(bearing)


def snapshot_from_scenario_trace(
    rows: Sequence[Mapping[str, object]],
    *,
    station: str,
    source: str,
    bundle_sha256: str,
    retrieved_at_us: int,
    expires_at_us: int | None = None,
    reference_course_mdeg: int = 90_000,
    wind_unit_is_metres_per_second: bool = True,
) -> WindSnapshot:
    """Freeze the scenario's own trace rows into the kernel's snapshot type.

    A deterministic run uses this and nothing else: the file is read by whoever
    builds the run, the result is a value, and no later stage can reach back out.
    """
    if not rows:
        raise TraceError("the scenario weather trace is empty")
    samples: list[tuple[int, int, int, int]] = []
    seen: set[int] = set()
    for index, row in enumerate(rows):
        minute = _field(row, "minute", index)
        if minute in seen:
            raise TraceError(f"scenario weather repeats minute {minute}")
        seen.add(minute)
        raw_speed = _field(row, "wind_ms", index)
        speed_dmps = raw_speed * 10 if wind_unit_is_metres_per_second else raw_speed
        headwind_bp = _field(row, "headwind_bp", index)
        gust_raw = row.get("gust_ms")
        if gust_raw is None:
            # The committed trace stores no gust column, so the sustained speed is
            # carried as the gust floor: no uplift, and no invented turbulence.
            gust_dmps = speed_dmps
        else:
            if isinstance(gust_raw, bool) or not isinstance(gust_raw, int):
                raise TraceError(f"scenario weather row {index} has no integer 'gust_ms'")
            gust_dmps = max(speed_dmps, gust_raw * 10)
        speed, bearing = _vector(speed_dmps, headwind_bp, reference_course_mdeg)
        samples.append((minute, speed, bearing, gust_dmps))
    return snapshot_from_rows(
        _deduplicated(samples),
        station=station,
        source=source,
        bundle_sha256=bundle_sha256,
        retrieved_at_us=retrieved_at_us,
        expires_at_us=expires_at_us,
    )


def _deduplicated(samples: Iterable[tuple[int, int, int, int]]) -> list[tuple[int, int, int, int]]:
    """Sort by minute and keep the first row per minute: a trace must be ordered."""
    ordered = sorted(samples, key=lambda row: row[0])
    kept: list[tuple[int, int, int, int]] = []
    for row in ordered:
        if kept and kept[-1][0] == row[0]:
            continue
        kept.append(row)
    return kept
