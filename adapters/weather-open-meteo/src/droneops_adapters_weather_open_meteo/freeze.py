"""Turn an Open-Meteo response into a frozen ``WindSnapshot``.

Pure functions of bytes already in hand: no client, no socket, no clock. The
conversion is where the meteorological convention is fixed once and for all -
Open-Meteo reports the direction the wind blows **from**, and
``droneops_energy.WindSample`` stores the direction it blows **towards**, because
that is what a wind triangle needs.

Units are integers end to end: kilometre-per-hour becomes deci-metre-per-second by
``km/h x 25 / 9`` rounded up (rounding up is the adverse direction for wind), and
degrees become millidegrees.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Final

from droneops_energy import WindSnapshot, normalize_mdeg, snapshot_from_rows
from droneops_energy.errors import EnergyInputError

#: Version of this adapter's own payload contract, recorded with each snapshot.
OPEN_METEO_SCHEMA_VERSION: Final = 1

#: Millidegrees in a half turn: Open-Meteo gives "from", the kernel wants "towards".
MDEG_PER_HALF_TURN: Final = 180_000

#: deci-metres per second per deci-kilometre-per-hour, as a fraction: x 5 / 18.
#: 1 km/h = 10/36 dm/s, so a speed read in tenths of a kilometre per hour needs no
#: float at all: multiply by five and divide by eighteen, rounding up.
KMH_TO_DMPS_NUMERATOR: Final = 5
KMH_TO_DMPS_DENOMINATOR: Final = 18

_MINUTES_PER_HOUR: Final = 60


class PayloadError(EnergyInputError):
    """The response body did not have the shape this adapter is contracted for."""


def _sequence(payload: Mapping[str, object], key: str) -> Sequence[object]:
    value = payload.get(key)
    if not isinstance(value, (list, tuple)):
        raise PayloadError(f"open-meteo payload has no list under {key!r}")
    return value


def _number(row: object, *, field: str, index: int) -> float:
    if isinstance(row, bool) or not isinstance(row, (int, float)):
        raise PayloadError(f"{field}[{index}] is not a number: {row!r}")
    return float(row)


def kmh_to_dmps(kmh: float) -> int:
    """Kilometres per hour to deci-metres per second, rounded up (the adverse direction)."""
    dkdh = round(kmh * 10)
    return -(-(dkdh * KMH_TO_DMPS_NUMERATOR) // KMH_TO_DMPS_DENOMINATOR)


def degrees_to_mdeg(degrees: float) -> int:
    """Compass degrees to normalised millidegrees."""
    return normalize_mdeg(round(degrees * 1000))


def _require_aligned(names: Sequence[str], lengths: Sequence[int]) -> None:
    if len(set(lengths)) != 1:
        raise PayloadError(
            "open-meteo hourly arrays are ragged: "
            + ", ".join(f"{name}={length}" for name, length in zip(names, lengths, strict=True))
        )


def snapshot_from_open_meteo(
    payload: Mapping[str, object],
    *,
    station: str,
    bundle_sha256: str,
    retrieved_at_us: int,
    expires_at_us: int | None = None,
) -> WindSnapshot:
    """Freeze an Open-Meteo hourly forecast into a replayable wind trace.

    ``bundle_sha256`` is the scenario bundle the run is pinned to: the snapshot
    records which frozen world it belongs to, so a snapshot from another run cannot
    be swapped in by accident.
    """
    hourly = payload.get("hourly")
    if not isinstance(hourly, Mapping):
        raise PayloadError("open-meteo payload has no 'hourly' object")
    rows: Mapping[str, object] = dict(hourly)
    times = _sequence(rows, "time")
    speeds = _sequence(rows, "wind_speed_10m")
    directions = _sequence(rows, "wind_direction_10m")
    gusts = _sequence(rows, "wind_gusts_10m")
    _require_aligned(
        ("time", "wind_speed_10m", "wind_direction_10m", "wind_gusts_10m"),
        [len(times), len(speeds), len(directions), len(gusts)],
    )
    if not times:
        raise PayloadError("open-meteo payload carries no hours")

    samples: list[tuple[int, int, int, int]] = []
    for index in range(len(times)):
        speed_dmps = kmh_to_dmps(_number(speeds[index], field="wind_speed_10m", index=index))
        gust_dmps = max(
            speed_dmps, kmh_to_dmps(_number(gusts[index], field="wind_gusts_10m", index=index))
        )
        from_mdeg = degrees_to_mdeg(
            _number(directions[index], field="wind_direction_10m", index=index)
        )
        samples.append(
            (
                index * _MINUTES_PER_HOUR,
                speed_dmps,
                normalize_mdeg(from_mdeg + MDEG_PER_HALF_TURN),
                gust_dmps,
            )
        )
    return snapshot_from_rows(
        samples,
        station=station,
        source=f"open-meteo frozen payload v{OPEN_METEO_SCHEMA_VERSION}",
        bundle_sha256=bundle_sha256,
        retrieved_at_us=retrieved_at_us,
        expires_at_us=expires_at_us,
    )


def snapshot_from_payload(
    body: str | bytes,
    *,
    station: str,
    bundle_sha256: str,
    retrieved_at_us: int,
    expires_at_us: int | None = None,
) -> WindSnapshot:
    """Decode a stored response body and freeze it: the replay entry point.

    This is the function a deterministic run uses. It takes the bytes that were
    saved at capture time, so the same payload always yields the same snapshot.
    """
    text = body.decode("utf-8") if isinstance(body, bytes) else body
    try:
        decoded = json.loads(text)
    except ValueError as error:
        raise PayloadError("stored weather payload is not JSON") from error
    if not isinstance(decoded, dict):
        raise PayloadError("stored weather payload is not a JSON object")
    return snapshot_from_open_meteo(
        decoded,
        station=station,
        bundle_sha256=bundle_sha256,
        retrieved_at_us=retrieved_at_us,
        expires_at_us=expires_at_us,
    )
