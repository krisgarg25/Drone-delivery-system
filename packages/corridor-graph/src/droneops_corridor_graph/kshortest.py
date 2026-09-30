"""Deterministic cost model and k-shortest candidate path generation.

Yen's algorithm over the directed lane graph, with the tie-breaking rule stated
where it can be tested (``technical-build/11`` section 11: "A-star plus safe
intervals - bounded and explainable"). Explainable means *identical output for
identical input*, so:

* every queue key is ``(cost, node_id, edge_id)`` - never a dict or set order, and
  never a float;
* costs are integers in **millimetre-equivalents**: distance is the declared length
  in millimetres, and time, band change, merge and crossing penalties are all
  converted into millimetre-equivalents by a documented rate per the policy;
* a lane closed for the whole plan window is *absent from the graph*, not expensive
  - a candidate that is merely penalised can still be selected under pressure,
  which would be an unenforced closure.

A restriction that expires mid-plan does **not** remove the lane: it is priced by
``edge_availability`` and the planner intersects the result with the usable
sub-windows, so a 30-38 minute closure leaves both flanking trips routable.
"""

from __future__ import annotations

import heapq
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from droneops_geom_core import Interval

from .errors import PathGenerationError
from .graph import CorridorGraph
from .model import CorridorEdge, canonical_digest

MM_PER_COST_UNIT: int = 1_000
"""One cost unit is one metre of declared lane: costs stay integers."""


@dataclass(frozen=True)
class CostProfile:
    """Penalties in cost units, all read from a versioned policy.

    ``crossing_penalty_units`` prices a lane that meets another lane elsewhere than
    a node, and ``merge_penalty_units`` a join at a merge node. ``band_change_units``
    prices a band transition because a climb is time and energy the corridor model
    cannot recover. None of them is a safety claim.
    """

    time_cost_units_per_second: int = 0
    band_change_units: int = 0
    merge_penalty_units: int = 0
    crossing_penalty_units: int = 0
    target_band: str | None = None
    off_target_band_units: int = 0

    def weight_for(
        self,
        edge: CorridorEdge,
        *,
        previous_band: str | None = None,
        at_merge: bool = False,
        crossings: int = 0,
    ) -> int:
        """Total integer cost of traversing one lane."""
        cost = edge.length_mm // MM_PER_COST_UNIT
        if self.time_cost_units_per_second:
            cost += (edge.cruise_time_us() // 1_000_000) * self.time_cost_units_per_second
        if previous_band is not None and previous_band != edge.band:
            cost += self.band_change_units
        if at_merge:
            cost += self.merge_penalty_units
        cost += crossings * self.crossing_penalty_units
        if self.target_band is not None and edge.band != self.target_band:
            cost += self.off_target_band_units
        return cost

    def profile_digest(self) -> str:
        return canonical_digest(self)


@dataclass(frozen=True)
class CorridorPath:
    """One candidate route: the lanes, their order and their measurable cost."""

    path_id: str
    edge_ids: tuple[str, ...]
    node_ids: tuple[str, ...]
    cost_units: int
    length_mm: int
    cruise_time_us: int
    bands: tuple[str, ...]
    merge_nodes: tuple[str, ...]
    crossing_count: int

    @property
    def edge_count(self) -> int:
        return len(self.edge_ids)

    def sort_key(self) -> tuple[int, tuple[str, ...]]:
        """The documented total order: cost first, then the lane sequence itself.

        Two equal-cost routes are separated by their lane ids, which is stable,
        human-checkable and independent of any iteration order.
        """
        return (self.cost_units, self.edge_ids)

    def canonical(self) -> str:
        """Stable identity of a route, so a re-run recognises its own answer."""
        return "|".join(self.edge_ids) or "empty"

    def digest(self) -> str:
        return canonical_digest(
            [
                self.edge_ids,
                self.cost_units,
                self.length_mm,
                self.cruise_time_us,
                self.bands,
                self.merge_nodes,
                self.crossing_count,
            ]
        )


@dataclass(frozen=True)
class CandidateSet:
    """k-shortest output: what is ranked, and what was removed and why."""

    start: str
    goal: str
    candidates: tuple[CorridorPath, ...]
    closed_lane_ids: tuple[str, ...]
    overlay_digest: str
    requested_k: int

    @property
    def exhausted(self) -> bool:
        """True when fewer than ``requested_k`` routes exist, which is a fact to report."""
        return len(self.candidates) < self.requested_k

    def ranking_digest(self) -> str:
        return canonical_digest([path.digest() for path in self.candidates])


def generate_candidates(
    graph: CorridorGraph,
    start: str,
    goal: str,
    *,
    k: int = 4,
    profile: CostProfile,
    window: Interval | None = None,
) -> CandidateSet:
    """The ``k`` cheapest loopless routes, in the documented total order.

    ``k`` is clamped by the caller's policy to the 3..5 band the pipeline expects;
    anything smaller than one is a configuration error, not an empty answer.

    Raises:
        PathGenerationError: unknown endpoint, a non-positive ``k``, or a goal that
            is unreachable once closures are applied.
    """
    if k < 1:
        raise PathGenerationError(f"k must be at least 1, got {k}")
    graph.node(start)
    graph.node(goal)
    if start == goal:
        raise PathGenerationError(f"origin and destination are both {start!r}")
    closed = graph.view_under_overlay(window.start, window.end) if window else None
    adjacency = closed.pruned_adjacency() if closed else graph.adjacency()
    removed = closed.fully_closed() if closed else ()
    if not graph.is_connected(start, goal):
        raise PathGenerationError(
            f"no lane sequence connects {start!r} to {goal!r} in this bundle"
        )
    shortest = _dijkstra(graph, adjacency, profile, start, goal)
    if shortest is None:
        raise PathGenerationError(
            f"closures removed every route from {start!r} to {goal!r}"
        )
    routes = [shortest]
    pool: list[CorridorPath] = []
    for iteration in range(1, k):
        previous = routes[-1]
        for spur_index in range(previous.edge_count):
            spur_node = previous.node_ids[spur_index]
            root_edges = previous.edge_ids[:spur_index]
            blocked_edges: set[str] = set()
            blocked_nodes: set[str] = set()
            for sibling in routes:
                if sibling.edge_ids[:spur_index] == root_edges and sibling.edge_count > spur_index:
                    blocked_edges.add(sibling.edge_ids[spur_index])
            blocked_nodes.update(node for node in previous.node_ids[:spur_index] if node != spur_node)
            spur = _dijkstra(
                graph,
                _blocked_view(adjacency, blocked_edges, blocked_nodes),
                profile,
                spur_node,
                goal,
            )
            if spur is None:
                continue
            candidate = _stitch(graph, profile, root_edges, spur)
            if candidate is None:
                continue
            known = {path.edge_ids for path in routes} | {path.edge_ids for path in pool}
            if candidate.edge_ids not in known:
                pool.append(candidate)
        if not pool:
            break
        pool.sort(key=CorridorPath.sort_key)
        routes.append(pool.pop(0))
        if len(routes) >= k:
            break
    return CandidateSet(
        start=start,
        goal=goal,
        candidates=tuple(routes[:k]),
        closed_lane_ids=tuple(sorted({item.edge_id for item in removed})),
        overlay_digest=graph.overlay.overlay_digest(),
        requested_k=k,
    )


def _stitch(
    graph: CorridorGraph,
    profile: CostProfile,
    root_edges: tuple[str, ...],
    spur: CorridorPath,
) -> CorridorPath | None:
    if any(edge_id in spur.edge_ids for edge_id in root_edges):
        return None
    return _rebuild(graph, profile, root_edges + spur.edge_ids)


def _rebuild(graph: CorridorGraph, profile: CostProfile, edge_ids: tuple[str, ...]) -> CorridorPath:
    """Recompute every measurable property of a route from the graph, in order."""
    edges = [graph.edge(edge_id) for edge_id in edge_ids]
    if not edges:
        raise PathGenerationError("cannot rebuild an empty route")
    nodes = [edges[0].from_node] + [edge.to_node for edge in edges]
    cost = 0
    previous_band: str | None = None
    merge_nodes: list[str] = []
    crossings = 0
    for index, edge in enumerate(edges):
        at_merge = graph.merge_for(nodes[index]) is not None and index > 0
        cost += profile.weight_for(
            edge, previous_band=previous_band, at_merge=at_merge,
            crossings=len(graph.crossing_partners(edge.edge_id)),
        )
        crossings += len(graph.crossing_partners(edge.edge_id))
        previous_band = edge.band
        if at_merge:
            merge_nodes.append(nodes[index])
    return CorridorPath(
        path_id=f"path-{canonical_digest(edge_ids)[7:23]}",
        edge_ids=tuple(edge_ids),
        node_ids=tuple(nodes),
        cost_units=cost,
        length_mm=sum(edge.length_mm for edge in edges),
        cruise_time_us=sum(edge.cruise_time_us() for edge in edges),
        bands=tuple(sorted({edge.band for edge in edges})),
        merge_nodes=tuple(sorted(set(merge_nodes))),
        crossing_count=crossings,
    )


def _blocked_view(
    adjacency: Mapping[str, tuple[tuple[str, str], ...]],
    blocked_edges: set[str],
    blocked_nodes: set[str],
) -> Mapping[str, tuple[tuple[str, str], ...]]:
    """Adjacency with spur-blocked lanes and root nodes removed, keys still sorted."""
    return {
        node_id: tuple(
            (to_node, edge_id)
            for to_node, edge_id in neighbours
            if edge_id not in blocked_edges and to_node not in blocked_nodes
        )
        for node_id, neighbours in sorted(adjacency.items())
    }


def _dijkstra(
    graph: CorridorGraph,
    adjacency: Mapping[str, tuple[tuple[str, str], ...]],
    profile: CostProfile,
    start: str,
    goal: str,
) -> CorridorPath | None:
    """Cheapest loop-free route under the documented tie-break.

    Queue entries are ``(cost, node_id, edge_path)`` so an equal-cost pair is
    settled by node id and then by the lane sequence, never by heap insertion
    order: ``heapq`` compares the tuple element by element and the path tuple is
    the final discriminator.
    """
    best: dict[str, tuple[int, tuple[str, ...]]] = {start: (0, ())}
    queue: list[tuple[int, str, tuple[str, ...], str | None]] = [(0, start, (), None)]
    settled: set[str] = set()
    while queue:
        cost, current, path, previous_band_token = heapq.heappop(queue)
        if current in settled:
            continue
        settled.add(current)
        if current == goal:
            return _rebuild(graph, profile, path)
        previous = best.get(current)
        if previous is not None and previous[0] < cost:
            continue
        previous_band = graph.edge(path[-1]).band if path else previous_band_token
        for to_node, edge_id in adjacency.get(current, ()):
            if to_node in settled:
                continue
            edge = graph.edge(edge_id)
            merge = graph.merge_for(current) is not None and bool(path)
            weight = profile.weight_for(
                edge,
                previous_band=previous_band,
                at_merge=merge,
                crossings=len(graph.crossing_partners(edge_id)),
            )
            total = cost + weight
            candidate_path = path + (edge_id,)
            recorded = best.get(to_node)
            if recorded is None or (total, candidate_path) < (recorded[0], recorded[1]):
                best[to_node] = (total, candidate_path)
                heapq.heappush(queue, (total, to_node, candidate_path, edge.band))
    return None


__all__ = [
    "MM_PER_COST_UNIT",
    "CandidateSet",
    "CorridorPath",
    "CostProfile",
    "generate_candidates",
]
