"""Ports: the narrow interfaces the pipeline depends on and does not own.

Each protocol is one seam where another package belongs:

* :class:`SweptVolumeBuilder` is the **geom-4d seam**. geom-4d is being written in
  parallel and owns swept envelopes, so the planner codes against this five-method
  contract instead of importing its unfinished internals;
* :class:`OccupancySource` and :class:`CommitPort` are the reservation ledger,
  owned by ``reservations`` and ``persistence`` - the planner asks, the ledger
  answers, and only the ledger commits;
* :class:`EnergyModel` is the ``energy`` package, :class:`FacilityScheduler` the
  ``facilities`` package, :class:`WindSource` a frozen bundle replay.

A ``Phase 0 stub`` package cannot be imported for real behaviour yet, so
``defaults.py`` ships reference implementations of each port that are correct for
the frozen synthetic world and replaceable without touching the pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from droneops_geom_core import Interval

from .types import (
    AircraftEnvelope,
    CommitOutcome,
    CommitRequest,
    EnergyEvaluation,
    PadWindow,
    ProtectedVolume,
    RouteShape,
)


@dataclass(frozen=True)
class VolumePadding:
    """The policy's margins, handed to the volume builder as data."""

    horizontal_mm: int
    vertical_mm: int
    uncertainty_mm: int
    timing_us: int
    transition_us: int
    separation_us: int

    def entry_padding_us(self, traversal_us: int) -> int:
        """How far an occupation blocks entry *before* it starts.

        The entry-blocking window is wider than the occupation by the traversal
        plus both buffers: an aircraft that enters too early is still inside when
        the committed traffic arrives.
        """
        return self.timing_us + self.transition_us + traversal_us


class OccupancySource(Protocol):
    """Committed occupancy, exactly as the ledger holds it."""

    def committed_windows(self, resource_identifier: str) -> tuple[tuple[int, int, str], ...]:
        """``(start_us, end_us, owner)`` for every commitment on this resource."""
        ...

    def reservation_version(self) -> int:
        """The monotonic ledger version this plan was prepared against."""
        ...

    def lane_usage_count(self, edge_id: str) -> int:
        """Commitments already held on a lane - the fairness input."""
        ...


class SweptVolumeBuilder(Protocol):
    """Continuous-movement protected volumes for one lane traversal (geom-4d)."""

    def sweep(
        self,
        edge_id: str,
        cells: tuple[str, ...],
        band: str,
        entry_us: int,
        exit_us: int,
        padding: VolumePadding,
    ) -> tuple[ProtectedVolume, ...]:
        """Volumes for the traversal, ascending by (window, cell set)."""
        ...

    def approach_volume(
        self,
        node_id: str,
        band_from: str,
        band_to: str,
        window: Interval,
        padding: VolumePadding,
    ) -> ProtectedVolume | None:
        """The climb or descent envelope through the layers between two bands."""
        ...


class CommitPort(Protocol):
    """The ledger transaction: one plan in, one answer out, all or nothing."""

    def try_commit(self, request: CommitRequest) -> CommitOutcome: ...


class EnergyModel(Protocol):
    """Consumption, reserve and recovery for a route in a wind state."""

    def evaluate(
        self,
        route: RouteShape,
        envelope: AircraftEnvelope,
        *,
        wind_mm_per_s: int,
        headwind_bp: int,
        payload_g: int,
        reserve_fraction_bp: int,
        recovery_length_mm: int,
        climb_mm: int,
        wind_energy_bp_per_ms: int,
    ) -> EnergyEvaluation:
        """Wind sensitivity is a policy number, so the port demands it rather than assuming one."""
        ...


class FacilityScheduler(Protocol):
    """Pad, charger and crew windows from declared capacity."""

    def pad_window(
        self,
        facility_id: str,
        purpose: str,
        *,
        not_before_us: int,
        duration_us: int,
        horizon: Interval,
    ) -> PadWindow | None: ...

    def charger_window(
        self,
        facility_id: str,
        *,
        not_before_us: int,
        duration_us: int,
        horizon: Interval,
    ) -> PadWindow | None: ...

    def crew_covers(self, facility_id: str, window: Interval) -> bool: ...


class WindSource(Protocol):
    """A frozen weather trace replayed as integers, never a live call."""

    def worst_wind_mm_per_s(self, window: Interval) -> int: ...

    def headwind_bp(self, window: Interval) -> int: ...


__all__ = [
    "CommitPort",
    "EnergyModel",
    "FacilityScheduler",
    "OccupancySource",
    "SweptVolumeBuilder",
    "VolumePadding",
    "WindSource",
]
