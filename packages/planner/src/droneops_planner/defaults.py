"""Reference port implementations: honest, integer-only, and replaceable.

These exist because ``geom-4d``, ``energy``, ``facilities`` and ``reservations`` are
either being built in parallel or are Phase 0 stubs. Each class here implements the
port in :mod:`droneops_planner.ports` for the frozen synthetic world, so steps 6 to
12 of the pipeline are *real computations* rather than placeholders, and each one
can be swapped for the owning package's implementation without touching the
pipeline.

They are deliberately conservative in the planner's own direction of error:
volumes are widened, energy estimates are ceilinged, and the commit port refuses
rather than guesses. The independent checker keeps its own implementation - that
separation is the point of section 6 of the conventions, not an accident.
"""

from __future__ import annotations

from dataclasses import dataclass

from droneops_corridor_graph import DEFAULT_GRID, CorridorGraph
from droneops_geom_core import (
    AltitudeReference,
    Cell3D,
    GridSpec,
    Interval,
    LocalPlane,
    gaps_within,
    overlaps_any,
)

from .model_util import BP_PER_WHOLE, MM_PER_METRE, apply_bp, ceil_div
from .ports import VolumePadding
from .reasons import ReasonCode
from .types import (
    AircraftEnvelope,
    CommitOutcome,
    CommitRequest,
    CommitState,
    EnergyEvaluation,
    PadWindow,
    ProtectedVolume,
    RouteShape,
)


class InMemoryOccupancy:
    """Committed occupancy as the ledger would hold it: sorted, versioned, queryable.

    Used by the simulator and by tests. It stores only what a commit actually
    granted, so a plan can never read back a window it invented.
    """

    def __init__(self) -> None:
        self._windows: dict[str, list[tuple[int, int, str]]] = {}
        self._lane_usage: dict[str, int] = {}
        self._version = 0

    def committed_windows(self, resource_identifier: str) -> tuple[tuple[int, int, str], ...]:
        held = self._windows.get(resource_identifier, [])
        return tuple(sorted(held, key=lambda item: (item[0], item[1], item[2])))

    def reservation_version(self) -> int:
        return self._version

    def lane_usage_count(self, edge_id: str) -> int:
        return self._lane_usage.get(edge_id, 0)

    def preload(
        self, resource_identifier: str, start_us: int, end_us: int, owner: str
    ) -> None:
        """Record traffic that already exists before this pipeline is asked to plan."""
        if end_us <= start_us:
            raise ValueError(f"preloaded window [{start_us}, {end_us}) is empty")
        self._windows.setdefault(resource_identifier, []).append((start_us, end_us, owner))
        self._version += 1

    def note_lane_usage(self, edge_id: str, count: int) -> None:
        self._lane_usage[edge_id] = count

    def grant(self, request: CommitRequest) -> None:
        """Apply an accepted commit: append windows, bump the version once."""
        for identifier, start_us, end_us in request.resources:
            self._windows.setdefault(identifier, []).append(
                (start_us, end_us, request.mission_id)
            )
            if identifier.startswith("lane-"):
                self._lane_usage[identifier] = self._lane_usage.get(identifier, 0) + 1
        self._version += 1


class InMemoryCommitPort:
    """A ledger stand-in with the two refusals the pipeline has to survive.

    It rejects a stale reservation version and it rejects an overlapping
    commitment; both are answers the pipeline is required to react to, so a test
    can drive the bounded recompute without a database.
    """

    def __init__(self, occupancy: InMemoryOccupancy) -> None:
        self.occupancy = occupancy
        self.attempts: list[CommitRequest] = []

    def try_commit(self, request: CommitRequest) -> CommitOutcome:
        self.attempts.append(request)
        observed = self.occupancy.reservation_version()
        if request.reservation_version != observed:
            return CommitOutcome(
                state=CommitState.REJECTED_STALE_VERSION,
                detail=(
                    f"plan prepared at version {request.reservation_version}, ledger is at "
                    f"{observed}"
                ),
                observed_version=observed,
            )
        for identifier, start_us, end_us in request.resources:
            clash = overlaps_any(
                Interval(start_us, end_us),
                (
                    Interval(item[0], item[1])
                    for item in self.occupancy.committed_windows(identifier)
                ),
            )
            if clash is not None:
                return CommitOutcome(
                    state=CommitState.REJECTED_CONFLICT,
                    detail=f"{identifier} already held for {clash}",
                    observed_version=observed,
                )
        self.occupancy.grant(request)
        return CommitOutcome(
            state=CommitState.COMMITTED,
            detail=f"{len(request.resources)} resources committed",
            observed_version=self.occupancy.reservation_version(),
        )


class CellSweepVolumeBuilder:
    """The planner's fast cell index: widen declared cells by the policy padding.

    This is the *planner* half of the two-implementation rule (spec section 7): a
    lattice expansion, not a geometric intersection. ``geom-4d`` owns the real
    swept envelope and replaces this class through
    :class:`~droneops_planner.ports.SweptVolumeBuilder` the moment it lands; the
    checker never uses it and must disagree nowhere.
    """

    def __init__(
        self,
        graph: CorridorGraph,
        *,
        grid: GridSpec | None = None,
        reference: AltitudeReference | None = None,
    ) -> None:
        self.graph = graph
        self.grid = grid or DEFAULT_GRID
        self.reference = reference or graph.metadata.altitude_reference
        self.plane: LocalPlane = graph.plane

    def sweep(
        self,
        edge_id: str,
        cells: tuple[str, ...],
        band: str,
        entry_us: int,
        exit_us: int,
        padding: VolumePadding,
    ) -> tuple[ProtectedVolume, ...]:
        """One volume per widened time step, holding the padded cell ring.

        The window is split into an entry-padded and an exit-padded claim so a
        checker sweeping the continuous segment sees a superset of what is
        reserved, never a subset.
        """
        ring = self._ring(cells, padding)
        duration = exit_us - entry_us
        if duration <= 0:
            raise ValueError(f"traversal window [{entry_us}, {exit_us}) is empty")
        mid_us = entry_us + duration // 2
        return (
            ProtectedVolume(edge_id, ring, Interval(entry_us, mid_us), band),
            ProtectedVolume(edge_id, ring, Interval(mid_us, exit_us), band),
        )

    def approach_volume(
        self,
        node_id: str,
        band_from: str,
        band_to: str,
        window: Interval,
        padding: VolumePadding,
    ) -> ProtectedVolume | None:
        """The climb or descent envelope over a node between two bands.

        Returns ``None`` when the bands are the same: there is no vertical
        transition to protect, and inventing a volume would overstate the claim.
        """
        if band_from == band_to:
            return None
        node = self.graph.node(node_id)
        offset = self.plane.offset_of(node.position)
        cells: set[str] = set()
        for band_index in self._band_span(band_from, band_to):
            cell = self.grid.volume_cell_of(
                offset.east_mm,
                offset.north_mm,
                band_index * self.grid.vertical_band_mm,
                level=self.grid.max_level,
                reference=self.reference,
            )
            cells.update(
                item.token()
                for item in cell.neighbourhood(radius=self._radius(padding))
            )
        return ProtectedVolume(
            f"approach-{node_id}",
            tuple(sorted(cells)),
            Interval(
                window.start - padding.timing_us - padding.transition_us,
                window.end + padding.timing_us + padding.transition_us,
            ),
            f"{band_from}>{band_to}",
        )

    def _ring(self, tokens: tuple[str, ...], padding: VolumePadding) -> tuple[str, ...]:
        radius = self._radius(padding)
        ring: set[str] = set()
        for token in tokens:
            cell = Cell3D.parse_token(self.grid, token, reference=self.reference)
            if radius <= 0:
                ring.add(token)
                continue
            ring.update(item.token() for item in cell.neighbourhood(radius=radius))
        return tuple(sorted(ring))

    def _radius(self, padding: VolumePadding) -> int:
        """Chebyshev ring width that covers the horizontal padding, in cells.

        Rounded up: an under-widened pre-filter is how two aircraft end up in one
        volume. The checker's continuous sweep is the backstop, not this.
        """
        span_mm = padding.horizontal_mm + padding.uncertainty_mm + padding.vertical_mm
        return max(0, ceil_div(span_mm, self.grid.cell_size_mm))

    def _band_span(self, band_from: str, band_to: str) -> tuple[int, ...]:
        rules = {rule.band: rule for rule in self.graph.bands.values()}
        low = min(rules[band_from].floor_mm, rules[band_to].floor_mm)
        high = max(rules[band_from].ceiling_mm, rules[band_to].ceiling_mm)
        band_height = self.grid.vertical_band_mm
        return tuple(
            range(low // band_height, ceil_div(high, band_height))
        )


@dataclass(frozen=True)
class BatteryCurveEnergyModel:
    """Cruise, payload, climb, wind and recovery in integer watt-hours.

    The curve is a declared bundle parameter, not a claim about a real airframe
    (conventions section 3). Every term is ceilinged and the payload and wind
    effects are multiplicative in per-mille integers, so a margin computed here can
    only be optimistic in the direction the checker will re-test.

    Units: ``wh_per_100km`` is the bundle's declared cruise rate, so
    ``cruise_wh = length_mm * rate / 1e8`` exactly.
    """

    payload_full_load_bp: int = 10_000
    """Extra cruise draw at maximum declared payload: +100 %, illustrative."""

    hover_climb_wh_per_1000m: int = 40
    """Energy to lift the aircraft one vertical kilometre, illustrative."""

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
        wind_energy_bp_per_ms: int = 250,
    ) -> EnergyEvaluation:
        """Consumption, reserve requirement and the remaining margin."""
        cruise_wh = ceil_div(route.length_mm * envelope.wh_per_100km, 100_000_000)
        payload_wh = (
            ceil_div(cruise_wh * payload_g * self.payload_full_load_bp, envelope.max_payload_g * BP_PER_WHOLE)
            if envelope.max_payload_g > 0
            else 0
        )
        climb_wh = ceil_div(climb_mm * self.hover_climb_wh_per_1000m, 1_000_000)
        wind_mps = wind_mm_per_s // MM_PER_METRE
        wind_wh = ceil_div(cruise_wh * wind_energy_bp_per_ms * wind_mps, BP_PER_WHOLE) if wind_mps else 0
        headwind_wh = ceil_div(cruise_wh * headwind_bp, BP_PER_WHOLE)
        recovery_wh = ceil_div(
            recovery_length_mm * envelope.wh_per_100km, 100_000_000
        )
        consumption_wh = cruise_wh + payload_wh + climb_wh + wind_wh + headwind_wh
        reserve_required_wh = ceil_div(envelope.capacity_wh * reserve_fraction_bp, BP_PER_WHOLE)
        return EnergyEvaluation(
            consumption_wh=consumption_wh,
            reserve_required_wh=reserve_required_wh,
            recovery_wh=recovery_wh,
            wind_penalty_wh=wind_wh + headwind_wh,
            climb_wh=climb_wh,
            available_wh=envelope.charge_wh,
            margin_wh=envelope.charge_wh - consumption_wh - recovery_wh - reserve_required_wh,
        )


class FrozenWindTrace:
    """A bundle weather trace replayed as integers over kernel time.

    The trace is a step function: one declared wind speed per minute. A worst-case
    reading across a window is what a conservative entry check needs, and it is
    taken from the frozen bundle only - there is no live fetch on this path.
    """

    def __init__(self, steps: tuple[tuple[int, int, int], ...], epoch_us: int) -> None:
        """``steps`` is ``(minute, wind_ms, headwind_bp)`` exactly as declared."""
        self._steps = tuple(
            sorted(
                (
                    (epoch_us + minute * 60 * 1_000_000, wind_ms * MM_PER_METRE, headwind_bp)
                    for minute, wind_ms, headwind_bp in steps
                ),
                key=lambda item: item[0],
            )
        )
        if not self._steps:
            raise ValueError("a frozen wind trace must declare at least one step")

    def worst_wind_mm_per_s(self, window: Interval) -> int:
        readings = [
            wind_us for start_us, wind_us, _ in self._steps if window.contains_instant(start_us)
        ]
        if readings:
            return max(readings)
        return self._step_at(window.start)

    def headwind_bp(self, window: Interval) -> int:
        readings = [
            headwind
            for start_us, _, headwind in self._steps
            if window.contains_instant(start_us)
        ]
        if readings:
            return max(readings)
        return self._headwind_at(window.start)

    def _step_at(self, at_us: int) -> int:
        previous = [step for step in self._steps if step[0] <= at_us]
        return previous[-1][1] if previous else self._steps[0][1]

    def _headwind_at(self, at_us: int) -> int:
        previous = [step for step in self._steps if step[0] <= at_us]
        return previous[-1][2] if previous else self._steps[0][2]


class BundleFacilityScheduler:
    """Pad and charger windows from the capacity the bundle declares.

    Allocation scans pad indices in ascending order and takes the first free
    sub-window of the required length, so two identical requests never land on the
    same pad and a full facility queues rather than overbooks.
    """

    def __init__(self, graph: CorridorGraph, occupancy: InMemoryOccupancy) -> None:
        self.graph = graph
        self.occupancy = occupancy

    def pad_window(
        self,
        facility_id: str,
        purpose: str,
        *,
        not_before_us: int,
        duration_us: int,
        horizon: Interval,
    ) -> PadWindow | None:
        node = self.graph.node(facility_id)
        attributes = node.facility
        if attributes is None:
            return None
        lower = max(not_before_us, attributes.open_us)
        upper = min(horizon.end, attributes.close_us)
        if upper - lower < duration_us:
            return None
        for index in range(attributes.pads):
            identifier = f"pad-{facility_id}-{index}"
            free = gaps_within(
                Interval(lower, upper),
                (
                    Interval(item[0], item[1])
                    for item in self.occupancy.committed_windows(identifier)
                ),
            )
            for candidate in free:
                if candidate.measure >= duration_us:
                    return PadWindow(
                        facility_id,
                        "pad",
                        index,
                        Interval(candidate.start, candidate.start + duration_us),
                        purpose,
                    )
        return None

    def charger_window(
        self,
        facility_id: str,
        *,
        not_before_us: int,
        duration_us: int,
        horizon: Interval,
    ) -> PadWindow | None:
        attributes = self.graph.node(facility_id).facility
        if attributes is None or attributes.charger_bays == 0:
            return None
        lower = max(not_before_us, attributes.open_us)
        upper = min(horizon.end, attributes.close_us)
        if upper - lower < duration_us:
            return None
        for index in range(attributes.charger_bays):
            identifier = f"charger-{facility_id}-{index}"
            free = gaps_within(
                Interval(lower, upper),
                (
                    Interval(item[0], item[1])
                    for item in self.occupancy.committed_windows(identifier)
                ),
            )
            for candidate in free:
                if candidate.measure >= duration_us:
                    return PadWindow(
                        facility_id, "charger_bay", index, Interval(candidate.start, candidate.start + duration_us), "charge"
                    )
        return None

    def crew_covers(self, facility_id: str, window: Interval) -> bool:
        attributes = self.graph.node(facility_id).facility
        if attributes is None or not attributes.crew_shifts:
            return True
        return any(shift.contains(window) for shift in attributes.crew_windows())


def rejection_reason(state: CommitState) -> ReasonCode:
    """Map a ledger refusal onto the planner's own reason code."""
    if state is CommitState.REJECTED_STALE_VERSION:
        return ReasonCode.COMMIT_STALE_VERSION
    if state is CommitState.REJECTED_CONFLICT:
        return ReasonCode.COMMIT_CONFLICT
    return ReasonCode.COMMIT_CONFLICT


__all__ = [
    "BatteryCurveEnergyModel",
    "BundleFacilityScheduler",
    "CellSweepVolumeBuilder",
    "FrozenWindTrace",
    "InMemoryCommitPort",
    "InMemoryOccupancy",
    "rejection_reason",
    "replace",
]
