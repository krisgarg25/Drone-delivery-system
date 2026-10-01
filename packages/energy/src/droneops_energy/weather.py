"""Frozen weather snapshots: the only way wind can reach the energy kernel.

The kernel's public entry points take a :class:`WindSnapshot` object. They cannot
take a station id, a URL, a client or a callable, so a deterministic run cannot
fetch: there is nothing in scope to fetch with (conventions section 4 - "external
feeds are replayed from the frozen bundle"). Adapters in ``adapters/weather-*``
turn a payload into a snapshot; this module only reads one.

Times inside a snapshot are minutes from the scenario epoch, and freshness is
judged against a caller-supplied integer microsecond clock - never a wall clock.

Wind convention, stated once: ``bearing_mdeg`` is the direction the wind is
blowing **towards** (not the meteorological "from" bearing), so a headwind on an
eastbound leg points west. Gusts and sustained wind are wired differently on
purpose:

* **sustained wind** is kinematic. It changes ground speed through the wind
  triangle, so a headwind lengthens the leg and the airframe burns power for
  longer. Its coefficients come from the profile and are illustrative.
* **gusts** are a policy uplift on the reserve, capped by ``gust_uplift_cap_bp``,
  because a peak gust is not a steady force a leg average can represent. That
  uplift is explicitly a *margin*, not a measured aerodynamic effect.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from itertools import pairwise

from .errors import EnergyInputError
from .numeric import BP_PER_UNIT, ceil_div
from .profile import ILLUSTRATIVE_DISCLAIMER
from .trig import MDEG_PER_TURN, normalize_mdeg

#: Microseconds in one minute of scenario time.
US_PER_MINUTE = 60_000_000

#: Provenance every snapshot must carry: it is never live data.
FROZEN_SOURCE_PREFIX = "frozen"


class Freshness(StrEnum):
    """How current a frozen snapshot is, judged against a supplied clock."""

    FRESH = "fresh"
    AGING = "aging"
    STALE = "stale"
    UNDATED = "undated"


@dataclass(frozen=True)
class WindSample:
    """Sustained wind and peak gust for one minute of the scenario epoch."""

    minute: int
    speed_dmps: int
    bearing_mdeg: int
    gust_dmps: int

    def __post_init__(self) -> None:
        if self.minute < 0:
            raise EnergyInputError("wind samples are minutes from the scenario epoch")
        if self.speed_dmps < 0 or self.gust_dmps < 0:
            raise EnergyInputError("wind speeds cannot be negative")
        if self.gust_dmps < self.speed_dmps:
            raise EnergyInputError(
                f"gust {self.gust_dmps} dm/s below sustained {self.speed_dmps} dm/s"
            )
        if not 0 <= self.bearing_mdeg < MDEG_PER_TURN:
            raise EnergyInputError("bearing_mdeg must be normalised to [0, 360000)")

    def with_speed_bp(self, factor_bp: int) -> WindSample:
        """Scale both speeds: used by sensitivity sweeps, rounding up (adverse)."""
        return replace(
            self,
            speed_dmps=ceil_div(self.speed_dmps * factor_bp, BP_PER_UNIT),
            gust_dmps=max(self.gust_dmps, ceil_div(self.gust_dmps * factor_bp, BP_PER_UNIT)),
        )

    def with_speed_delta(self, delta_dmps: int) -> WindSample:
        """Add a signed speed delta, floored at zero calm."""
        return replace(
            self,
            speed_dmps=max(0, self.speed_dmps + delta_dmps),
            gust_dmps=max(self.gust_dmps + delta_dmps, 0, self.speed_dmps + delta_dmps),
        )

    def reversed(self) -> WindSample:
        """The same speed on the opposite course: a wind-reversal stress case."""
        return replace(self, bearing_mdeg=normalize_mdeg(self.bearing_mdeg + MDEG_PER_TURN // 2))


@dataclass(frozen=True)
class WindSnapshot:
    """An immutable wind trace with provenance - a scenario artefact, not a feed."""

    station: str
    source: str
    samples: tuple[WindSample, ...]
    bundle_sha256: str
    retrieved_at_us: int
    expires_at_us: int | None = None
    authoritative: bool = False
    schema_version: int = 1

    def __post_init__(self) -> None:
        if not self.samples:
            raise EnergyInputError("a wind snapshot needs at least one sample")
        if not self.station or not self.source:
            raise EnergyInputError("a wind snapshot needs a station and a source")
        if len(self.bundle_sha256) != 64:
            raise EnergyInputError("bundle_sha256 must be the scenario bundle content hash")
        if self.authoritative:
            raise EnergyInputError(
                "a weather snapshot cannot be authoritative in Phase 3: it is a frozen "
                "simulation artefact and is not an operational forecast"
            )
        ordered = sorted(self.samples, key=lambda row: row.minute)
        if any(a.minute == b.minute for a, b in pairwise(ordered)):
            raise EnergyInputError("wind samples must have distinct minutes")
        if len(self.samples) > 1 and list(self.samples) != ordered:
            raise EnergyInputError("wind samples must be ordered by ascending minute")

    # ------------------------------------------------------------- access
    @property
    def span_minutes(self) -> int:
        """Minutes covered by the trace."""
        return self.samples[-1].minute - self.samples[0].minute

    @property
    def basis(self) -> str:
        """The basis statement carried on every derived result."""
        return ILLUSTRATIVE_DISCLAIMER

    def sample_at_minute(self, minute: int) -> WindSample:
        """The most recent sample at or before ``minute``, held at both ends.

        Holding the first and last sample is a deliberate choice: a trace that
        runs out mid-mission must not become calm, and it must not fail silently.
        """
        chosen = self.samples[0]
        for row in self.samples:
            if row.minute <= minute:
                chosen = row
            else:
                break
        return chosen

    def sample_at_us(self, elapsed_us: int) -> WindSample:
        """Sample for an elapsed scenario time in integer microseconds."""
        return self.sample_at_minute(elapsed_us // US_PER_MINUTE)

    def window(self, start_us: int, end_us: int) -> tuple[WindSample, ...]:
        """Samples in effect over ``[start_us, end_us]``, in trace order.

        A window that falls between two samples resolves to the governing sample
        rather than to nothing, so gust statistics never silently read as calm.
        """
        first_minute = start_us // US_PER_MINUTE
        last_minute = max(first_minute, end_us // US_PER_MINUTE)
        rows = tuple(row for row in self.samples if first_minute <= row.minute <= last_minute)
        if rows:
            return rows
        return (self.sample_at_minute(first_minute),)

    def peak_gust_dmps(self, start_us: int, end_us: int) -> int:
        """Worst gust over the mission window: feeds the reserve uplift, not burn."""
        rows = self.window(start_us, end_us)
        if not rows:
            rows = (self.sample_at_us(start_us), self.sample_at_us(end_us))
        return max(row.gust_dmps for row in rows)

    def peak_sustained_dmps(self, start_us: int, end_us: int) -> int:
        """Worst sustained speed over the mission window, in deci-metres per second."""
        rows = self.window(start_us, end_us)
        if not rows:
            return self.sample_at_us(start_us).speed_dmps
        return max(row.speed_dmps for row in rows)

    def mean_sustained_dmps(self, start_us: int, end_us: int) -> int:
        """Time-weighted mean sustained speed over a window, rounded up."""
        rows = self.window(start_us, end_us)
        if not rows:
            return self.sample_at_us(start_us).speed_dmps
        total = sum(row.speed_dmps for row in rows)
        return ceil_div(total, len(rows))

    # ---------------------------------------------------------- provenance
    def freshness(self, as_of_us: int) -> Freshness:
        """Age class against a caller-supplied integer clock, never ``time``.

        ``as_of_us`` is the scenario clock at the point of decision; the kernel
        has no way to ask what time it is.
        """
        if self.expires_at_us is None:
            return Freshness.UNDATED
        if as_of_us > self.expires_at_us:
            return Freshness.STALE
        horizon = self.expires_at_us - self.retrieved_at_us
        if horizon <= 0:
            return Freshness.STALE
        if as_of_us - self.retrieved_at_us > horizon * 3 // 4:
            return Freshness.AGING
        return Freshness.FRESH

    def is_admissible(self, as_of_us: int) -> bool:
        """Only a fresh or aging trace may drive a feasibility number.

        A stale snapshot means the wind is unknown, and unknown wind is
        uncertainty rather than calm (conventions section 2).
        """
        return self.freshness(as_of_us) in {Freshness.FRESH, Freshness.AGING, Freshness.UNDATED}

    # ------------------------------------------------- deterministic re-use
    def with_samples(self, samples: Iterable[WindSample]) -> WindSnapshot:
        """A new snapshot with different samples and the same provenance."""
        rows: Sequence[WindSample] = tuple(samples)
        return replace(self, samples=tuple(rows))

    def scaled_by(self, factor_bp: int) -> WindSnapshot:
        """Scale every wind speed: how a sensitivity sweep strengthens the wind."""
        return self.with_samples(row.with_speed_bp(factor_bp) for row in self.samples)

    def shifted_by(self, delta_dmps: int) -> WindSnapshot:
        """Add a signed wind speed to every sample, in deci-metres per second."""
        return self.with_samples(row.with_speed_delta(delta_dmps) for row in self.samples)

    def reversed(self) -> WindSnapshot:
        """Flip every wind direction: the same route with the weather behind it."""
        return self.with_samples(row.reversed() for row in self.samples)


def snapshot_from_rows(
    rows: Iterable[tuple[int, int, int, int]],
    *,
    station: str,
    source: str,
    bundle_sha256: str,
    retrieved_at_us: int,
    expires_at_us: int | None = None,
) -> WindSnapshot:
    """Build a snapshot from ``(minute, speed_dmps, bearing_mdeg, gust_dmps)`` rows.

    Adapters own parsing; this constructor owns the invariants, so a malformed
    trace cannot enter a run.
    """
    samples = tuple(
        WindSample(minute=m, speed_dmps=s, bearing_mdeg=normalize_mdeg(b), gust_dmps=g)
        for m, s, b, g in sorted(rows, key=lambda row: row[0])
    )
    if not samples:
        raise EnergyInputError("an empty wind trace cannot support a feasibility decision")
    return WindSnapshot(
        station=station,
        source=source,
        samples=samples,
        bundle_sha256=bundle_sha256,
        retrieved_at_us=retrieved_at_us,
        expires_at_us=expires_at_us,
    )
