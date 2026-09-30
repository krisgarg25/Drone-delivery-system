"""The geom-4d seam, implemented against the real swept-volume kernel.

``ports.SweptVolumeBuilder`` is the one place the planner asks for protected
volumes, and this module answers it with ``droneops_geom_4d`` rather than with a
lattice guess: a :class:`~droneops_geom_4d.RouteProfile` is built from the declared
centreline vertices and the safe-interval entry time, ``sweep_profile`` produces the
continuous swept prisms, ``volume_cells``/``index_volumes`` rasterise them onto
``geom-core`` cells, and ``climb_descent_envelopes`` prices the band transition. The
policy margins are the versioned numbers from the release config; nothing here
invents a margin (conventions section 3).

Why this exists beside :class:`~droneops_planner.defaults.CellSweepVolumeBuilder`
    the cell builder is the planner's own conservative pre-filter (it widens the
    declared lane cells and never claims a shape), while this builder is the payload
    a proposal publishes: real geometry, real padding, re-derivable point by point by
    anything that can read the bundle. Both satisfy the same port, so switching the
    pipeline between them is a wiring decision, not a rewrite.

Ordering: volumes are returned ascending by ``(window.start, edge_id)``, cell tokens
ascending by token, profile points ascending by time.
"""

from __future__ import annotations

from collections.abc import Sequence

from droneops_corridor_graph import DEFAULT_GRID, CorridorEdge, CorridorGraph
from droneops_geom_4d import (
    SUPPORTED_SCHEMA_VERSION,
    IndexedVolume,
    ProfilePoint,
    ProtectedBody,
    ProtectedPrism,
    ProtectedVolumePolicy,
    RouteProfile,
    VolumeIndex,
    climb_descent_envelopes,
    index_volumes,
    sweep_profile,
    volume_cells,
)
from droneops_geom_core import (
    AltitudeReference,
    Cell3D,
    GridSpec,
    Interval,
    as_microseconds,
    as_millimetres,
    geodesic_distance_mm,
)

from .ports import VolumePadding
from .space_time import EdgeTiming
from .types import ProtectedVolume

MAX_INDEX_CELLS = 4096
"""Cell budget for one rasterised prism, the same ceiling ``geom-4d`` enforces."""

DEFAULT_INDEX_LEVEL_DROP = 3
"""How far above the graph's finest cell level the swept index sits.

Rasterising a swept prism at the graph's finest level is exact but costs thousands of
cells per leg on a 10 m grid, and a pre-filter that cannot be computed is not a
pre-filter. The drop is a resolution knob, stated here rather than buried in a call:
callers that intend to *reserve* these cells pass ``level=grid.max_level`` so the
tokens match the ones the ledger holds, and callers that only publish the swept
claim use the default.
"""


def policy_from_snapshot(
    graph: CorridorGraph,
    *,
    policy_version: str,
    horizontal_margin_mm: int,
    vertical_margin_mm: int,
    position_uncertainty_mm: int,
    timing_uncertainty_us: int,
    transition_buffer_us: int,
    reference: AltitudeReference | None = None,
) -> ProtectedVolumePolicy:
    """The kernel's policy object, assembled from the release config this plan used.

    ``authoritative`` is fixed false: these margins are experiment parameters, and a
    synthetic policy must never present itself as a certified one.
    """
    return ProtectedVolumePolicy(
        schema_version=SUPPORTED_SCHEMA_VERSION,
        policy_version=policy_version,
        authoritative=False,
        altitude_reference=reference or graph.metadata.altitude_reference,
        horizontal_margin_mm=as_millimetres(horizontal_margin_mm),
        vertical_margin_mm=as_millimetres(vertical_margin_mm),
        position_uncertainty_mm=as_millimetres(position_uncertainty_mm),
        timing_uncertainty_us=as_microseconds(timing_uncertainty_us),
        transition_buffer_us=as_microseconds(transition_buffer_us),
    )


def lane_profile(
    graph: CorridorGraph,
    edge: CorridorEdge,
    *,
    entry_us: int,
    motion_end_us: int,
    plan_version: str,
    source: str,
) -> RouteProfile:
    """A continuous 4D profile for one lane traversal, from declared geometry only.

    The centreline vertices are time-stamped in proportion to their geodesic length,
    so the profile describes the same path the bundle declares rather than a
    straight chord between its endpoints. Times are integer microseconds; a vertex
    that would land on a fractional instant is rounded down and the next one inherits
    the remainder, which keeps the last vertex exactly on ``motion_end_us``.
    """
    vertices = edge.geometry
    spans = [
        geodesic_distance_mm(vertices[index].point, vertices[index + 1].point)
        for index in range(len(vertices) - 1)
    ]
    total_mm = sum(spans) or edge.length_mm
    duration_us = motion_end_us - entry_us
    if duration_us <= 0:
        raise ValueError(f"lane {edge.edge_id} traversal window is empty")
    speed_mmps = max(1, edge.length_mm * 1_000_000 // duration_us)
    points = [
        ProfilePoint.at(
            entry_us,
            vertices[0].point.lat_deg,
            vertices[0].point.lon_deg,
            vertices[0].altitude_mm,
            vertices[0].reference,
            speed_mmps,
        )
    ]
    travelled_mm = 0
    for index, span in enumerate(spans):
        travelled_mm += span
        at_us = entry_us + (duration_us * travelled_mm) // total_mm
        vertex = vertices[index + 1]
        last = index == len(spans) - 1
        points.append(
            ProfilePoint.at(
                motion_end_us if last else max(at_us, points[-1].at_us + 1),
                vertex.point.lat_deg,
                vertex.point.lon_deg,
                vertex.altitude_mm,
                vertex.reference,
                speed_mmps,
            )
        )
    return RouteProfile.from_points(
        points,
        plan_version=plan_version,
        source=source,
        authoritative=False,
        plane=graph.plane,
    )


def route_profile(
    graph: CorridorGraph,
    edge_ids: tuple[str, ...],
    timings: Sequence[EdgeTiming],
    *,
    plan_version: str,
    source: str,
) -> RouteProfile:
    """The whole traversal as one continuous 4D profile: the payload a checker re-sweeps.

    Each lane contributes its declared vertices, time-stamped between the entry and
    exit the safe-interval search settled on. Consecutive legs do not share a
    waypoint, because the aircraft dwells at the junction for the separation buffer:
    dropping that dwell would understate how long the route occupies the volume.
    """
    points: list[ProfilePoint] = []
    for record, edge_id in zip(timings, edge_ids, strict=True):
        leg = lane_profile(
            graph,
            graph.edge(edge_id),
            entry_us=record.entry_us,
            motion_end_us=record.exit_us,
            plan_version=plan_version,
            source=source,
        )
        points.extend(leg.points)
    if len(points) < 2:
        raise ValueError("a route profile needs at least two waypoints")
    return RouteProfile.from_points(
        points,
        plan_version=plan_version,
        source=source,
        authoritative=False,
        plane=graph.plane,
    )


class Geom4dSweepVolumeBuilder:
    """``SweptVolumeBuilder`` backed by the swept-volume kernel, not by a lattice guess.

    Each traversal becomes a profile, each profile a continuous swept prism, each
    prism the set of ``geom-core`` volume cells it rasterises onto. The padding the
    pipeline already applied in the safe-interval search is *not* re-applied here:
    this builder is given the motion window and returns the padded claim, which is
    what the ledger should hold.
    """

    def __init__(
        self,
        graph: CorridorGraph,
        policy: ProtectedVolumePolicy,
        body: ProtectedBody,
        *,
        grid: GridSpec | None = None,
        level: int | None = None,
        max_cells: int = MAX_INDEX_CELLS,
    ) -> None:
        self.graph = graph
        self.policy = policy
        self.body = body
        self.grid = grid or DEFAULT_GRID
        self.level = (
            max(0, self.grid.max_level - DEFAULT_INDEX_LEVEL_DROP) if level is None else level
        )
        self.max_cells = max_cells
        self.prisms: dict[str, tuple[ProtectedPrism, ...]] = {}

    def sweep(
        self,
        edge_id: str,
        cells: tuple[str, ...],
        band: str,
        entry_us: int,
        exit_us: int,
        padding: VolumePadding,
    ) -> tuple[ProtectedVolume, ...]:
        """The padded swept claim for one lane traversal, ascending by window.

        ``exit_us`` is the release instant the safe-interval search already settled
        on, so the motion window is that minus the two temporal buffers.
        """
        del cells
        edge = self.graph.edge(edge_id)
        motion_end_us = exit_us - padding.timing_us - padding.transition_us
        if motion_end_us <= entry_us:
            motion_end_us = exit_us - padding.timing_us
        if motion_end_us <= entry_us:
            raise ValueError(f"lane {edge_id} has no positive motion window")
        profile = lane_profile(
            self.graph,
            edge,
            entry_us=entry_us,
            motion_end_us=motion_end_us,
            plan_version=self.policy.policy_version,
            source=self.graph.metadata.source,
        )
        prisms = sweep_profile(profile, self.body, self.policy)
        self.prisms[edge_id] = prisms
        return tuple(
            sorted(
                (
                    ProtectedVolume(
                        edge_id,
                        self._tokens(prism),
                        prism.window(),
                        band,
                    )
                    for prism in prisms
                ),
                key=lambda item: (item.window.start, item.edge_id),
            )
        )

    def approach_volume(
        self,
        node_id: str,
        band_from: str,
        band_to: str,
        window: Interval,
        padding: VolumePadding,
    ) -> ProtectedVolume | None:
        """The climb or descent envelope between two bands, from the kernel's manoeuvre code.

        ``None`` for a same-band transition: there is no manoeuvre to protect.
        """
        del padding
        if band_from == band_to:
            return None
        rules = self.graph.bands
        low = rules[band_from]
        high = rules[band_to]
        reference = self.graph.metadata.altitude_reference
        node = self.graph.node(node_id)
        from_altitude_mm = min(low.floor_mm, high.floor_mm)
        to_altitude_mm = max(low.ceiling_mm, high.ceiling_mm)
        half_us = max(1, (window.end - window.start) // 2)
        profile = RouteProfile.from_points(
            (
                ProfilePoint.at(
                    window.start,
                    node.position.lat_deg,
                    node.position.lon_deg,
                    from_altitude_mm,
                    reference,
                    0,
                ),
                ProfilePoint.at(
                    window.start + half_us,
                    node.position.lat_deg,
                    node.position.lon_deg,
                    to_altitude_mm,
                    reference,
                    0,
                ),
                ProfilePoint.at(
                    window.end,
                    node.position.lat_deg,
                    node.position.lon_deg,
                    from_altitude_mm,
                    reference,
                    0,
                ),
            ),
            plan_version=self.policy.policy_version,
            source=self.graph.metadata.source,
            authoritative=False,
            plane=self.graph.plane,
        )
        prisms = sweep_profile(profile, self.body, self.policy)
        envelopes = climb_descent_envelopes(profile, self.body, self.policy)
        tokens = sorted({token for prism in prisms for token in self._tokens(prism)})
        if not tokens:
            return None
        # The cells come from the swept prisms; the window from the manoeuvre
        # envelopes, because a climb is a transition and carries the buffer.
        start_us = min(
            (envelope.window.start for envelope in envelopes),
            default=min((prism.window().start for prism in prisms), default=window.start),
        )
        end_us = max(
            (envelope.window.end for envelope in envelopes),
            default=max((prism.window().end for prism in prisms), default=window.end),
        )
        return ProtectedVolume(
            f"approach-{node_id}",
            tuple(tokens),
            Interval.of(start_us, end_us),
            f"{band_from}>{band_to}",
        )

    def index(self) -> VolumeIndex:
        """The cell-and-window pre-filter for everything this builder has swept.

        Step 8 asks exactly this question of the committed set; publishing it means
        a reviewer can see which pairs the index could not rule out.
        """
        prisms = [
            prism for _edge_id, swept in sorted(self.prisms.items()) for prism in swept
        ]
        return index_volumes(prisms, self.grid, level=self.level, max_cells=self.max_cells)

    def _tokens(self, prism: ProtectedPrism) -> tuple[str, ...]:
        return tuple(sorted(cell.token() for cell in self.cells_of(prism)))

    def cells_of(self, prism: ProtectedPrism) -> tuple[Cell3D, ...]:
        """Cells one swept prism rasterises onto, at the graph's index precision."""
        return volume_cells(prism, self.grid, level=self.level, max_cells=self.max_cells)

    def indexed_volumes(self) -> tuple[IndexedVolume, ...]:
        return self.index().volumes


__all__ = [
    "MAX_INDEX_CELLS",
    "Geom4dSweepVolumeBuilder",
    "lane_profile",
    "policy_from_snapshot",
    "route_profile",
]
