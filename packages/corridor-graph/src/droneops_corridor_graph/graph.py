"""The corridor graph: directed lanes, adjacency, crossings and resource keys.

A graph is an immutable value: the same bundle bytes always produce the same
adjacency, the same crossing set and the same digest, because every collection it
exposes is rebuilt in a documented order at construction (conventions section 4
forbids unsorted dict/set iteration from producing output).

Reservation identity lives here: :meth:`CorridorGraph.edge_resource_key` and
:meth:`CorridorGraph.edge_volume_cell_keys` are the only place a lane becomes a
``geom-core`` :class:`~droneops_geom_core.ResourceKey`, so the ledger, the planner
and the checker cannot each invent a different spelling of the same volume.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from droneops_geom_core import (
    Cell3D,
    Interval,
    LocalPlane,
    ResourceKey,
    ResourceKind,
    lock_order,
    resource_key,
    sorted_resource_keys,
)

from .errors import TopologyError
from .geometry import PlanarSegment, segment_offsets
from .model import (
    AltitudeBandRule,
    CorridorEdge,
    CorridorNode,
    GraphSourceMetadata,
    MergeKind,
    MergeNode,
    canonical_digest,
)
from .overlay import ClosureOverlay, Restriction


@dataclass(frozen=True)
class CorridorGraph:
    """One versioned, directed lane graph loaded from one frozen bundle."""

    metadata: GraphSourceMetadata
    nodes: Mapping[str, CorridorNode]
    edges: Mapping[str, CorridorEdge]
    bands: Mapping[str, AltitudeBandRule]
    overlay: ClosureOverlay
    plane: LocalPlane
    edge_cells: Mapping[str, tuple[Cell3D, ...]]
    scenario_title: str
    _outbound: Mapping[str, tuple[CorridorEdge, ...]] = field(init=False, repr=False)
    _inbound: Mapping[str, tuple[CorridorEdge, ...]] = field(init=False, repr=False)
    _merges: tuple[MergeNode, ...] = field(init=False, repr=False)
    _crossings: tuple[tuple[str, str], ...] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        edges = {edge_id: self.edges[edge_id] for edge_id in sorted(self.edges)}
        nodes = {node_id: self.nodes[node_id] for node_id in sorted(self.nodes)}
        for edge in edges.values():
            for endpoint in (edge.from_node, edge.to_node):
                if endpoint not in nodes:
                    raise TopologyError(
                        f"lane {edge.edge_id} names endpoint {endpoint!r} which the "
                        "facilities document does not declare"
                    )
        object.__setattr__(self, "edges", edges)
        object.__setattr__(self, "nodes", nodes)
        object.__setattr__(
            self,
            "_outbound",
            _index(nodes, sorted(edges.values(), key=CorridorEdge.sort_key), "from_node"),
        )
        object.__setattr__(
            self,
            "_inbound",
            _index(nodes, sorted(edges.values(), key=CorridorEdge.sort_key), "to_node"),
        )
        object.__setattr__(self, "_merges", self._build_merges())
        object.__setattr__(self, "_crossings", self._build_crossings())

    @property
    def node_ids(self) -> tuple[str, ...]:
        return tuple(self.nodes)

    @property
    def edge_ids(self) -> tuple[str, ...]:
        return tuple(self.edges)

    @property
    def graph_version(self) -> str:
        return self.metadata.graph_version

    @property
    def bundle_sha256(self) -> str:
        return self.metadata.bundle_sha256

    def node(self, node_id: str) -> CorridorNode:
        try:
            return self.nodes[node_id]
        except KeyError as error:
            raise TopologyError(f"unknown node {node_id!r}") from error

    def edge(self, edge_id: str) -> CorridorEdge:
        try:
            return self.edges[edge_id]
        except KeyError as error:
            raise TopologyError(f"unknown lane {edge_id!r}") from error

    def band_rule(self, band: str) -> AltitudeBandRule:
        try:
            return self.bands[band]
        except KeyError as error:
            raise TopologyError(f"unknown altitude band {band!r}") from error

    def out_edges(self, node_id: str) -> tuple[CorridorEdge, ...]:
        """Lanes leaving a node, ordered ``(to_node, edge_id)``."""
        self.node(node_id)
        return self._outbound[node_id]

    def in_edges(self, node_id: str) -> tuple[CorridorEdge, ...]:
        """Lanes arriving at a node, ordered ``(from_node, edge_id)``."""
        self.node(node_id)
        return self._inbound[node_id]

    def adjacency(self) -> Mapping[str, tuple[tuple[str, str], ...]]:
        """``node -> ((next_node, edge_id), ...)``, keys and values ascending."""
        return {
            node_id: tuple((edge.to_node, edge.edge_id) for edge in self._outbound[node_id])
            for node_id in self.nodes
        }

    def reachable_from(self, start: str) -> frozenset[str]:
        """Node ids reachable by lane, computed in sorted order for determinism."""
        seen: set[str] = set()
        frontier = [start]
        while frontier:
            current = min(frontier)
            frontier.remove(current)
            if current in seen:
                continue
            seen.add(current)
            frontier.extend(edge.to_node for edge in self.out_edges(current))
        return frozenset(seen)

    def is_connected(self, start: str, goal: str) -> bool:
        return goal in self.reachable_from(start)

    def merge_nodes(self) -> tuple[MergeNode, ...]:
        """Nodes where lanes join or split, ascending by node id."""
        return self._merges

    def merge_for(self, node_id: str) -> MergeNode | None:
        for merge in self._merges:
            if merge.node_id == node_id:
                return merge
        return None

    def crossings(self) -> tuple[tuple[str, str], ...]:
        """Canonical ``(low, high)`` lane pairs whose centrelines cross, sorted."""
        return self._crossings

    def crossing_partners(self, edge_id: str) -> tuple[str, ...]:
        self.edge(edge_id)
        return tuple(
            sorted(
                {
                    other
                    for low, other in self._crossings
                    if low == edge_id
                }
                | {
                    low
                    for low, high in self._crossings
                    if high == edge_id
                }
            )
        )

    def crossing_nodes_between(self, path: Sequence[str]) -> tuple[str, ...]:
        """Nodes on a path whose inbound and outbound lanes belong to different groups."""
        nodes: list[str] = []
        for index in range(1, len(path) - 1):
            nodes.append(path[index])
        return tuple(sorted(set(nodes)))

    def edge_resource_key(self, edge_id: str) -> ResourceKey:
        """The bare corridor key: identity of the lane, with no window."""
        return resource_key(ResourceKind.CORRIDOR, self._identifier(edge_id))

    def edge_volume_cell_keys(
        self, edge_id: str, start_us: int, end_us: int
    ) -> tuple[ResourceKey, ...]:
        """One time-bounded volume-cell key per cell the declared centreline crosses.

        This is the reservation surface: ``corridor:`` carries no window in the
        canonical scheme, so an edge traversal reserves its cells. Order is the
        canonical lock order, which is what makes a multi-cell commit deadlock-free.
        """
        cells = self.edge_cells_for(edge_id)
        keys = [
            resource_key(
                ResourceKind.VOLUME_CELL,
                self._cell_identifier(cell),
                Interval(start_us, end_us),
            )
            for cell in cells
        ]
        return sorted_resource_keys(keys)

    def edge_cells_for(self, edge_id: str) -> tuple[Cell3D, ...]:
        self.edge(edge_id)
        return self.edge_cells[edge_id]

    def plan_lock_order(self, edge_ids: Sequence[str], start_us: int, end_us: int) -> tuple[str, ...]:
        """Canonical acquisition strings for a whole route."""
        keys: list[ResourceKey] = []
        for edge_id in edge_ids:
            keys.extend(self.edge_volume_cell_keys(edge_id, start_us, end_us))
        return lock_order(keys)

    def total_length_mm(self) -> int:
        return sum(edge.length_mm for edge in self.edges.values())

    def lane_directions(self) -> Mapping[str, int]:
        """Counts per direction class - the shape a reviewer checks against the bundle."""
        counts: dict[str, int] = {}
        for edge in self.edges.values():
            counts[edge.lane_direction.value] = counts.get(edge.lane_direction.value, 0) + 1
        return {key: counts[key] for key in sorted(counts)}

    def topology_digest(self) -> str:
        """Content hash of nodes, lanes, bands and overlay - the graph's identity."""
        return canonical_digest(
            [
                self.metadata.scenario_id,
                [node.node_id for node in self.nodes.values()],
                [
                    (edge.edge_id, edge.lane_group_id, edge.lane_direction.value)
                    for edge in self.edges.values()
                ],
                [rule.band for rule in self.bands.values()],
                self.overlay.overlay_digest(),
            ]
        )

    def view_under_overlay(self, window_start_us: int, window_end_us: int) -> "OverlayView":
        """A time-scoped read of the graph: which lanes are enterable and when."""
        return OverlayView(self, window_start_us, window_end_us)

    def _identifier(self, edge_id: str) -> str:
        edge = self.edge(edge_id)
        return _canonical_identifier(f"{edge.lane_group_id}-{edge.to_node}")

    def _cell_identifier(self, cell: Cell3D) -> str:
        return _canonical_identifier(f"{cell.token()}-{cell.reference.value}")

    def _build_merges(self) -> tuple[MergeNode, ...]:
        merges: list[MergeNode] = []
        for node_id in self.nodes:
            inbound = tuple(edge.edge_id for edge in self._inbound[node_id])
            outbound = tuple(edge.edge_id for edge in self._outbound[node_id])
            if len(inbound) + len(outbound) < 2:
                continue
            kind = _merge_kind(len(inbound), len(outbound))
            if kind is MergeKind.THROUGH:
                continue
            conflicts = tuple(
                sorted(
                    {
                        (arriving, departing)
                        for arriving in inbound
                        for departing in outbound
                        if self.edges[arriving].lane_group_id
                        != self.edges[departing].lane_group_id
                    }
                )
            )
            merges.append(
                MergeNode(
                    node_id=node_id,
                    kind=kind,
                    inbound=inbound,
                    outbound=outbound,
                    conflict_pairs=conflicts,
                )
            )
        return tuple(sorted(merges, key=MergeNode.sort_key))

    def _build_crossings(self) -> tuple[tuple[str, str], ...]:
        pairs: list[tuple[str, str]] = []
        ordered = sorted(self.edges.values(), key=CorridorEdge.sort_key)
        for left_index, left in enumerate(ordered):
            for right in ordered[left_index + 1 :]:
                if {left.from_node, left.to_node} & {right.from_node, right.to_node}:
                    continue
                if left.lane_group_id == right.lane_group_id:
                    continue
                if _lanes_cross(self, left, right):
                    pairs.append((left.edge_id, right.edge_id))
        return tuple(sorted(set(pairs)))


@dataclass(frozen=True)
class OverlayView:
    """The graph as it stands inside one plan window: enterable lanes, and why not."""

    graph: CorridorGraph
    start_us: int
    end_us: int

    @property
    def window(self) -> Interval:
        return Interval(self.start_us, self.end_us)

    def fully_closed(self) -> tuple[Restriction, ...]:
        """Closures that remove candidates outright for this whole window."""
        return tuple(
            sorted(
                (
                    item
                    for item in self.graph.overlay.restrictions
                    if item.blocks_window(self.window)
                ),
                key=Restriction.sort_key,
            )
        )

    def usable_out_edges(self, node_id: str) -> tuple[CorridorEdge, ...]:
        return tuple(
            edge
            for edge in self.graph.out_edges(node_id)
            if not self.graph.overlay.is_closed_throughout(edge.edge_id, self.window)
        )

    def availability(self, edge_id: str) -> tuple[Interval, ...]:
        """Sub-windows in which the lane is enterable: non-empty even when it
        reopens mid-plan, which is the whole point of a time-scoped view."""
        return self.graph.overlay.availability_intervals(edge_id, self.window)

    def pruned_adjacency(self) -> Mapping[str, tuple[tuple[str, str], ...]]:
        """Adjacency with lanes closed for the entire window removed."""
        closed = {
            item.edge_id
            for item in self.fully_closed()
        }
        return {
            node_id: tuple(
                (to_node, edge_id)
                for to_node, edge_id in self.graph.adjacency()[node_id]
                if edge_id not in closed
            )
            for node_id in sorted(self.graph.nodes)
        }


def _merge_kind(inbound: int, outbound: int) -> MergeKind:
    if inbound > 1 and outbound > 1:
        return MergeKind.INTERSECTION
    if inbound > 1:
        return MergeKind.CONVERGENCE
    if outbound > 1:
        return MergeKind.DIVERGENCE
    return MergeKind.THROUGH


def _index(
    nodes: Mapping[str, CorridorNode], edges: Sequence[CorridorEdge], attribute: str
) -> Mapping[str, tuple[CorridorEdge, ...]]:
    buckets: dict[str, list[CorridorEdge]] = {node_id: [] for node_id in nodes}
    for edge in edges:
        buckets[getattr(edge, attribute)].append(edge)
    return {node_id: tuple(buckets[node_id]) for node_id in sorted(buckets)}


def _lanes_cross(graph: CorridorGraph, left: CorridorEdge, right: CorridorEdge) -> bool:
    left_segments = segment_offsets(left, graph.plane)
    right_segments = segment_offsets(right, graph.plane)
    return any(a.crosses(b) for a in left_segments for b in right_segments)


def _canonical_identifier(text: str) -> str:
    """Fold a bundle id into the canonical resource-key alphabet, collision-safely.

    ``geom-core`` accepts ``[A-Za-z0-9._~-]`` up to 64 characters. Anything else -
    the ``+`` of a bidirectional lane group, a long synthetic name - is replaced,
    and an over-long result is truncated with a digest suffix so two different
    lanes can never be folded onto one reservation row.
    """
    slugged = "".join(
        character if character.isalnum() or character in ".-_~" else "-"
        for character in text
    )
    if len(slugged) <= _IDENTIFIER_LIMIT:
        return slugged
    digest = canonical_digest(slugged)
    return f"{slugged[: _IDENTIFIER_LIMIT - 17]}-{digest[7:23]}"


_IDENTIFIER_LIMIT = 64

__all__ = [
    "CorridorGraph",
    "OverlayView",
]
