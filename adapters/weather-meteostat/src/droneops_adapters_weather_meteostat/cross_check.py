"""Parse a Meteostat station report and cross-check it against a frozen snapshot."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from droneops_energy import WindSnapshot
from droneops_energy.errors import EnergyInputError


class Verdict(StrEnum):
    """Whether the second source agrees with the trace this run replays."""

    AGREES = "agrees"
    DRIFTS = "drifts"
    CONTRADICTS = "contradicts"
    NO_OVERLAP = "no_overlap"


class ReportError(EnergyInputError):
    """The station payload did not have the contracted shape."""


@dataclass(frozen=True)
class StationReport:
    """Minute-indexed sustained wind from a ground station, with its provenance."""

    station: str
    source: str
    speeds_dmps: tuple[tuple[int, int], ...]
    retrieved_at_us: int
    authoritative: bool = False

    def __post_init__(self) -> None:
        if self.authoritative:
            raise ReportError(
                "a Meteostat cross-check is never authoritative: it is a second opinion "
                "on a frozen simulation trace, not an operational observation"
            )
        minutes = [minute for minute, _speed in self.speeds_dmps]
        if minutes != sorted(minutes):
            raise ReportError("station report minutes must be ascending")
        if len(set(minutes)) != len(minutes):
            raise ReportError("station report repeats a minute")

    @classmethod
    def from_rows(
        cls,
        rows: Sequence[tuple[int, int]],
        *,
        station: str,
        source: str,
        retrieved_at_us: int,
    ) -> StationReport:
        """Build a report from ``(minute, speed_dmps)`` rows."""
        cleaned = tuple(sorted((minute, max(0, speed)) for minute, speed in rows))
        return cls(
            station=station,
            source=source,
            speeds_dmps=cleaned,
            retrieved_at_us=retrieved_at_us,
        )


@dataclass(frozen=True)
class CrossCheckResult:
    """The outcome of comparing two sources. It informs; it decides nothing."""

    verdict: Verdict
    minutes_compared: int
    max_deviation_dmps: int
    mean_deviation_dmps: int
    tolerance_dmps: int
    note: str

    @property
    def blocks_review(self) -> bool:
        """Whether a reviewer should look at this before trusting the trace."""
        return self.verdict in {Verdict.DRIFTS, Verdict.CONTRADICTS, Verdict.NO_OVERLAP}

    @property
    def summary(self) -> str:
        return (
            f"{self.verdict.value}: {self.minutes_compared} minutes compared, "
            f"max deviation {self.max_deviation_dmps} dm/s against a tolerance of "
            f"{self.tolerance_dmps} dm/s"
        )


def report_from_payload(
    payload: Mapping[str, object],
    *,
    station: str,
    retrieved_at_us: int,
    source: str = "meteostat frozen station report",
) -> StationReport:
    """Freeze a stored Meteostat daily/hourly payload into a report.

    Wind arrives in kilometre-per-hour and becomes deci-metre-per-second rounded up,
    the same adverse rounding the forecast adapter uses, so the two sources are
    compared on identical units.
    """
    hourly = payload.get("hourly")
    if not isinstance(hourly, Mapping):
        raise ReportError("meteostat payload has no 'hourly' object")
    rows = dict(hourly)
    speeds = rows.get("wspd") or rows.get("wind_speed")
    if not isinstance(speeds, (list, tuple)):
        raise ReportError("meteostat payload has no wind-speed list")
    pairs = tuple(
        # kilometre-per-hour to deci-metre-per-second is x 1000 / 360, rounded up.
        (index * 60, -(-round(float(value) * 1000) // 360))
        for index, value in enumerate(speeds)
    )
    return StationReport.from_rows(
        pairs,
        station=station,
        source=source,
        retrieved_at_us=retrieved_at_us,
    )


def cross_check_snapshot(
    snapshot: WindSnapshot,
    report: StationReport,
    *,
    tolerance_dmps: int = 20,
) -> CrossCheckResult:
    """Compare the frozen trace with a second source, minute by minute.

    Only minutes both sources cover are compared: a gap is reported rather than
    smoothed over, because "no second opinion" is not "agreement".
    """
    if tolerance_dmps < 0:
        raise ReportError("a cross-check tolerance cannot be negative")
    by_minute = {sample.minute: sample.speed_dmps for sample in snapshot.samples}
    reported = dict(report.speeds_dmps)
    shared = sorted(set(by_minute) & set(reported))
    if not shared:
        return CrossCheckResult(
            verdict=Verdict.NO_OVERLAP,
            minutes_compared=0,
            max_deviation_dmps=0,
            mean_deviation_dmps=0,
            tolerance_dmps=tolerance_dmps,
            note="the second source covers no minute this trace does",
        )
    deviations = [abs(by_minute[minute] - reported[minute]) for minute in shared]
    worst = max(deviations)
    mean = sum(deviations) // len(deviations)
    if worst <= tolerance_dmps:
        verdict = Verdict.AGREES
    elif worst <= tolerance_dmps * 3:
        verdict = Verdict.DRIFTS
    else:
        verdict = Verdict.CONTRADICTS
    return CrossCheckResult(
        verdict=verdict,
        minutes_compared=len(shared),
        max_deviation_dmps=worst,
        mean_deviation_dmps=mean,
        tolerance_dmps=tolerance_dmps,
        note=(
            "cross-check only: the frozen snapshot stays the basis of the run, and "
            "no reservation depends on this result"
        ),
    )
