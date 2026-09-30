"""Space-time evaluation: safe intervals, swept volumes, pre-filter, precise overlap.

This is steps 6 to 9 of ``technical-build/11`` section 3, and it is why the engine
does not use a time-expanded graph (section 4): instead of multiplying every node by
every time bucket, each candidate asks *when may this aircraft enter this lane
without intersecting a commitment* and gets back a small set of windows.

Ordering rules, because the answer is evidence:

* occupied windows are labelled with the mission that holds them, and the merged
  blocker labels come straight out of ``geom-core``, so a rejection names the
  traffic that caused it;
* per-cell windows are intersected in cell-token order;
* the route chain walks edges in travel order and takes the earliest admissible
  entry at each step, so the same route and the same ledger always produce the same
  entry times.
"""

from __future__ import annotations

from dataclasses import dataclass

from droneops_corridor_graph import CorridorEdge, CorridorGraph
from droneops_geom_core import (
    Interval,
    OccupiedWindow,
    SafeIntervalPadding,
    SafeIntervalQuery,
    SafeIntervalResult,
    SafeWindow,
    compute_safe_intervals,
    intersect_all,
    merge,
)

from .model_util import canonical_digest
from .ports import OccupancySource, SweptVolumeBuilder, VolumePadding
from .reasons import ReasonCode, Rejection
from .types import ProtectedVolume


@dataclass(frozen=True)
class EdgeTiming:
    """Where a route enters and leaves one lane, and what blocked the alternatives."""

    edge_id: str
    entry_us: int
    exit_us: int
    release_us: int
    admissible: tuple[Interval, ...]
    blocked_by: tuple[str, ...]

    def digest(self) -> str:
        return canonical_digest(
            [self.edge_id, self.entry_us, self.exit_us, self.release_us, self.blocked_by]
        )


@dataclass(frozen=True)
class RouteTiming:
    """A feasible traversal of a whole route, or the first edge that failed."""

    timings: tuple[EdgeTiming, ...]
    entry_us: int
    exit_us: int
    release_us: int

    @property
    def duration_us(self) -> int:
        return self.release_us - self.entry_us


@dataclass(frozen=True)
class OverlapReport:
    """Step 8 and 9 output: what was pre-filtered, what actually intersected."""

    prefilter_hits: tuple[tuple[str, str, int, int], ...]
    conflicts: tuple[tuple[str, str, Interval], ...]

    @property
    def is_clear(self) -> bool:
        return not self.conflicts

    def digest(self) -> str:
        return canonical_digest(
            [
                [list(item) for item in self.prefilter_hits],
                [[item[0], item[1], item[2].start, item[2].end] for item in self.conflicts],
            ]
        )


def padding_of(
    *, horizontal_mm: int, vertical_mm: int, uncertainty_mm: int,
    timing_us: int, transition_us: int, separation_us: int,
) -> VolumePadding:
    """Assemble the policy's margins into one value object."""
    return VolumePadding(
        horizontal_mm=horizontal_mm,
        vertical_mm=vertical_mm,
        uncertainty_mm=uncertainty_mm,
        timing_us=timing_us,
        transition_us=transition_us,
        separation_us=separation_us,
    )


def safe_intervals_for_edge(
    graph: CorridorGraph,
    edge: CorridorEdge,
    *,
    horizon: Interval,
    earliest_us: int,
    padding: VolumePadding,
    occupancy: OccupancySource,
) -> tuple[SafeWindow, ...]:
    """Entry windows admissible on *every* cell the lane crosses.

    One query per volume cell, then an intersection: a lane is enterable only when
    all of its cells are enterable at the same entry instant.
    """
    geom_padding = SafeIntervalPadding(
        uncertainty_us=padding.timing_us, transition_buffer_us=padding.transition_us
    )
    traversal_us = edge.cruise_time_us()
    start_us = max(earliest_us, horizon.start)
    if horizon.end <= start_us:
        return ()
    window = Interval(start_us, horizon.end)
    if window.is_empty:
        return ()
    per_cell: list[tuple[SafeWindow, ...]] = []
    for key in graph.edge_volume_cell_keys(edge.edge_id, window.start, window.end):
        identifier = key.identifier
        occupied = tuple(
            # One commitment can hold two adjacent halves of one padded traversal;
            # geom-core requires a distinct label per occupied window, so the label
            # carries its own start and the owner stays readable in any audit.
            OccupiedWindow(Interval(start_us, end_us), f"{owner}@{start_us}", key)
            for start_us, end_us, owner in sorted(
                occupancy.committed_windows(identifier), key=lambda item: (item[0], item[1], item[2])
            )
        )
        query = SafeIntervalQuery(key, window, traversal_us, geom_padding, occupied)
        per_cell.append(compute_safe_intervals(query).windows)
    if not per_cell:
        return ()
    return _intersect_windows(per_cell)


def chain_route_timing(
    graph: CorridorGraph,
    edge_ids: tuple[str, ...],
    *,
    horizon: Interval,
    earliest_us: int,
    padding: VolumePadding,
    occupancy: OccupancySource,
    separation_us: int,
) -> RouteTiming | Rejection:
    """Walk the route in travel order taking the earliest admissible entry per lane.

    Returns a :class:`Rejection` rather than raising when a lane has no admissible
    entry: "why not" is the result the decision trace needs, including which
    mission's commitment closed the window.
    """
    timings: list[EdgeTiming] = []
    cursor = earliest_us
    for edge_id in edge_ids:
        edge = graph.edge(edge_id)
        windows = safe_intervals_for_edge(
            graph, edge, horizon=horizon, earliest_us=cursor, padding=padding, occupancy=occupancy
        )
        available = tuple(window.entry for window in windows)
        chosen = _first_window_containing(windows, cursor)
        if chosen is None:
            scan_from = min(cursor, horizon.end)
            scan_to = max(scan_from + 1, horizon.end)
            return Rejection(
                subject=f"route:{'>'.join(edge_ids)}",
                code=ReasonCode.NO_SAFE_WINDOW,
                detail=(
                    f"lane {edge_id} has no admissible entry at or after {cursor} us within "
                    f"[{horizon.start}, {horizon.end}); cells checked: "
                    f"{', '.join(sorted({key.identity_token() for key in graph.edge_volume_cell_keys(edge_id, scan_from, scan_to)}))}"
                ),
                graph_version=graph.graph_version,
                evidence=tuple(
                    sorted(
                        {
                            owner
                            for cell in graph.edge_cells_for(edge_id)
                            for start_us, end_us, owner in occupancy.committed_windows(
                                f"{cell.token()}-{cell.reference.value}"
                            )
                        }
                    )
                ),
            )
        entry_us = max(cursor, chosen.earliest_entry)
        exit_us = entry_us + edge.cruise_time_us()
        release_us = exit_us + padding.timing_us + padding.transition_us
        timings.append(
            EdgeTiming(
                edge_id=edge_id,
                entry_us=entry_us,
                exit_us=exit_us,
                release_us=release_us,
                admissible=available,
                blocked_by=_blockers(graph, edge_id, entry_us, exit_us, occupancy),
            )
        )
        cursor = release_us + separation_us
    if not timings:
        return Rejection(
            subject="route:empty",
            code=ReasonCode.NO_ROUTE_FOUND,
            detail="the route carried no lane to traverse",
            graph_version=graph.graph_version,
        )
    return RouteTiming(
        timings=tuple(timings),
        entry_us=timings[0].entry_us,
        exit_us=timings[-1].exit_us,
        release_us=timings[-1].release_us,
    )


def build_volumes(
    graph: CorridorGraph,
    timing: RouteTiming,
    *,
    padding: VolumePadding,
    builder: SweptVolumeBuilder,
) -> tuple[ProtectedVolume, ...]:
    """Step 7: swept protected volumes for every lane, plus band-change approaches."""
    volumes: list[ProtectedVolume] = []
    previous_band: str | None = None
    for record in timing.timings:
        edge = graph.edge(record.edge_id)
        cells = tuple(cell.token() for cell in graph.edge_cells_for(record.edge_id))
        volumes.extend(
            builder.sweep(
                record.edge_id,
                cells,
                edge.band,
                record.entry_us,
                record.release_us,
                padding,
            )
        )
        if previous_band is not None and previous_band != edge.band:
            approach = builder.approach_volume(
                edge.from_node,
                previous_band,
                edge.band,
                Interval(record.entry_us, record.exit_us),
                padding,
            )
            if approach is not None:
                volumes.append(approach)
        previous_band = edge.band
    return tuple(sorted(volumes, key=lambda item: (item.window.start, item.edge_id)))


def prefilter_and_intersect(
    graph: CorridorGraph,
    volumes: tuple[ProtectedVolume, ...],
    occupancy: OccupancySource,
) -> OverlapReport:
    """Steps 8 and 9: cheap cell-and-time-window screen, then precise intersection.

    The screen asks only "does this committed window share a cell token and touch
    this padded window". The precise pass then intersects the actual interval sets,
    so a near miss is reported as a near miss and not as a conflict.
    """
    hits: list[tuple[str, str, int, int]] = []
    conflicts: list[tuple[str, str, Interval]] = []
    for volume in volumes:
        for token in volume.cells:
            identifier = _cell_identifier(token, graph.metadata.altitude_reference.value)
            committed = occupancy.committed_windows(identifier)
            for start_us, end_us, owner in committed:
                candidate = Interval(start_us, end_us)
                if not candidate.overlaps(volume.window):
                    continue
                hits.append((identifier, owner, start_us, end_us))
                exact = intersect_all((volume.window, candidate))
                if exact is not None and not exact.is_empty:
                    conflicts.append((identifier, owner, exact))
        for edge_id, _owner, _window in _lane_level_clashes(graph, volume, occupancy):
            conflicts.append((f"lane-{edge_id}", _owner, _window))
    return OverlapReport(
        prefilter_hits=tuple(sorted(hits)), conflicts=tuple(sorted(conflicts, key=_conflict_key))
    )


def _lane_level_clashes(
    graph: CorridorGraph, volume: ProtectedVolume, occupancy: OccupancySource
) -> tuple[tuple[str, str, Interval], ...]:
    """Commitments recorded against the bare lane identity, if the ledger uses one."""
    if volume.edge_id not in graph.edges:
        return ()
    identifier = graph.edge_resource_key(volume.edge_id).identifier
    found: list[tuple[str, str, Interval]] = []
    for start_us, end_us, owner in occupancy.committed_windows(identifier):
        window = Interval(start_us, end_us)
        exact = intersect_all((volume.window, window))
        if exact is not None and not exact.is_empty:
            found.append((volume.edge_id, owner, exact))
    return tuple(found)


def _cell_identifier(token: str, reference_value: str) -> str:
    return f"{token}-{reference_value}"


def _conflict_key(item: tuple[str, str, Interval]) -> tuple[str, str, int, int]:
    identifier, owner, window = item
    return (identifier, owner, window.start, window.end)


def _first_window_containing(
    windows: tuple[SafeWindow, ...], not_before_us: int
) -> SafeWindow | None:
    """The earliest window whose admissible entry range reaches ``not_before_us``."""
    for window in windows:
        if window.latest_entry >= not_before_us and window.entry.end > not_before_us:
            return window
    return None


def _intersect_windows(
    per_cell: list[tuple[SafeWindow, ...]],
) -> tuple[SafeWindow, ...]:
    """Intersect entry ranges across cells, keeping the tightest bounding traffic."""
    entry_sets = [merge(tuple(window.entry for window in group)) for group in per_cell]
    combined = entry_sets[0]
    for following in entry_sets[1:]:
        combined = _intersect_interval_sets(combined, following)
    reference = per_cell[0]
    kept: list[SafeWindow] = []
    for interval in combined:
        parent = next(
            (item for item in reference if item.entry.contains(interval)), reference[0]
        )
        kept.append(
            SafeWindow(
                interval,
                Interval(interval.start + parent.traversal_us, interval.end + parent.traversal_us),
                Interval(
                    interval.start + parent.traversal_us + parent.release.measure,
                    interval.end + parent.traversal_us + parent.release.measure,
                ),
                parent.traversal_us,
                parent.bounded_before,
                parent.bounded_after,
            )
        )
    return tuple(kept)


def _intersect_interval_sets(
    left: tuple[Interval, ...], right: tuple[Interval, ...]
) -> tuple[Interval, ...]:
    pieces = [
        exact
        for a in left
        for b in right
        if (exact := intersect_all((a, b))) is not None and not exact.is_empty
    ]
    return merge(pieces)


def _blockers(
    graph: CorridorGraph,
    edge_id: str,
    entry_us: int,
    exit_us: int,
    occupancy: OccupancySource,
) -> tuple[str, ...]:
    """Missions holding traffic on this lane's cells at or before this entry.

    Recorded on every leg so a decision trace can answer "who was in the way"
    without re-running the search.
    """
    reference = graph.metadata.altitude_reference.value
    found: set[str] = set()
    for cell in graph.edge_cells_for(edge_id):
        for start_us, end_us, owner in occupancy.committed_windows(f"{cell.token()}-{reference}"):
            if start_us <= exit_us and end_us >= entry_us:
                found.add(owner)
    return tuple(sorted(found))


def blockers_of(result: SafeIntervalResult) -> tuple[str, ...]:
    """Mission labels whose merged occupation produced the blocked windows."""
    return tuple(sorted({item.label for item in result.query.occupied}))


__all__ = [
    "EdgeTiming",
    "OverlapReport",
    "RouteTiming",
    "blockers_of",
    "build_volumes",
    "chain_route_timing",
    "padding_of",
    "prefilter_and_intersect",
    "resource_key",
    "ResourceKind",
    "safe_intervals_for_edge",
]
