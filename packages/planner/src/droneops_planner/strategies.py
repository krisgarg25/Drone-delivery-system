"""Three routing strategies, comparable under identical demand.

``technical-build/05`` Phase 3 and ``docs/05-chandigarh-demo.md`` section 5.6 require
constraint-aware direct routing, structured corridor routing and a hybrid to be
compared on the *same* demand, weather, aircraft states, seed, cutoff, energy
profiles and capacity. An intentionally weakened baseline would make that
comparison misleading, so all three share everything except how a candidate path is
proposed:

* **structured corridor** - Yen k-shortest over the declared lane graph.
* **constraint-aware direct** - point-to-point over a connector lattice built from
  node geometry. It is not "unconstrained": a connector pays the geometric crossing
  penalty like any other lane, it is checked against the same energy, weather,
  deadline and protected-volume gates in steps 6 to 10, and the same safe-interval
  search decides when it may be entered.
* **hybrid** - lane edges and connector edges in one graph, so the cost model itself
  decides where lane structure is worth its detour.

The tie-break is the same documented total order in all three, and the connector
lattice is derived from the loaded graph rather than hard-coded.
"""

from __future__ import annotations

from droneops_corridor_graph import (
    CandidateSet,
    ClosureOverlay,
    CorridorEdge,
    CorridorGraph,
    CostProfile,
    GeometryPoint,
    LaneDirection,
    LaneRule,
    generate_candidates,
)
from droneops_corridor_graph.geometry import swept_cells
from droneops_geom_core import AltitudeReference, Interval, geodesic_distance_mm

from .model_util import canonical_digest
from .reasons import PipelineConfigError
from .types import PlanRequest

DIRECT_PREFIX = "direct"


class RoutingStrategy:
    """Base: candidate generation over the declared lane graph."""

    name = "structured_corridor"
    explanation = "k-shortest over declared one-way and bidirectional lanes only"

    def graph_for(self, graph: CorridorGraph) -> CorridorGraph:
        return graph

    def propose(
        self,
        graph: CorridorGraph,
        origin: str,
        destination: str,
        *,
        k: int,
        profile: CostProfile,
        horizon: Interval,
    ) -> CandidateSet:
        return generate_candidates(
            self.graph_for(graph), origin, destination, k=k, profile=profile, window=horizon
        )


class StructuredCorridorStrategy(RoutingStrategy):
    """Use the structure the operator actually published."""

    name = "structured_corridor"
    explanation = "k-shortest over declared one-way and bidirectional lanes only"


class ConstraintAwareDirectStrategy(RoutingStrategy):
    """Point-to-point over a connector lattice, with the identical constraint gates."""

    name = "constraint_aware_direct"
    explanation = "geodesic connector lattice; same safe-interval, volume and energy gates"

    def __init__(self, lattice: CorridorGraph) -> None:
        self.lattice = lattice

    def graph_for(self, graph: CorridorGraph) -> CorridorGraph:
        return self.lattice


class HybridRoutingStrategy(RoutingStrategy):
    """Lanes plus connectors: the cost model chooses the structure."""

    name = "hybrid"
    explanation = "declared lanes and geodesic connectors in one graph"

    def __init__(self, combined: CorridorGraph) -> None:
        self.combined = combined

    def graph_for(self, graph: CorridorGraph) -> CorridorGraph:
        return self.combined


def connector_lattice(
    graph: CorridorGraph,
    *,
    include_lanes: bool,
    reference: AltitudeReference | None = None,
) -> CorridorGraph:
    """Build the direct-connect graph from node geometry, not from a hard-coded list.

    A connector joins any two nodes the lane network already knows about, at the
    highest declared band. Its width, wind cap and speed cap are taken from the
    declared lanes - the widest, the most permissive and the slowest - so a
    connector is never given a cheaper flight condition than a real lane for reasons
    unrelated to its length.
    """
    used_reference = reference or graph.metadata.altitude_reference
    band = _highest_band(graph)
    rule_band = graph.band_rule(band)
    widest = max(edge.rule.width_mm for edge in graph.edges.values())
    wind_cap = max(edge.rule.max_wind_mm_per_s for edge in graph.edges.values())
    speed_cap = min(edge.rule.speed_cap_mm_per_s for edge in graph.edges.values())
    edges: dict[str, CorridorEdge] = {}
    if include_lanes:
        edges.update(dict(graph.edges))
    node_ids = sorted(graph.nodes)
    for origin in node_ids:
        for destination in node_ids:
            if origin == destination:
                continue
            edge_id = f"{DIRECT_PREFIX}-{origin}-{destination}"
            origin_node = graph.node(origin)
            destination_node = graph.node(destination)
            edges[edge_id] = CorridorEdge(
                edge_id=edge_id,
                from_node=origin,
                to_node=destination,
                lane_group_id=edge_id,
                lane_direction=LaneDirection.ONE_WAY,
                band=band,
                length_mm=geodesic_distance_mm(origin_node.position, destination_node.position),
                rule=LaneRule(
                    speed_cap_mm_per_s=speed_cap,
                    max_wind_mm_per_s=wind_cap,
                    width_mm=widest,
                    band=band,
                ),
                geometry=(
                    GeometryPoint(
                        origin_node.position, rule_band.floor_mm, origin_node.reference
                    ),
                    GeometryPoint(
                        destination_node.position, rule_band.floor_mm, destination_node.reference
                    ),
                ),
                reference=used_reference,
                synthetic=True,
            )
    plane = graph.plane
    return CorridorGraph(
        metadata=graph.metadata,
        nodes=dict(graph.nodes),
        edges=edges,
        bands=dict(graph.bands),
        overlay=ClosureOverlay.empty() if not include_lanes else graph.overlay,
        plane=plane,
        edge_cells={edge_id: swept_cells(edge, plane) for edge_id, edge in edges.items()},
        scenario_title=graph.scenario_title,
    )


def _highest_band(graph: CorridorGraph) -> str:
    return max(
        (rule.band for rule in graph.bands.values()),
        key=lambda band: (graph.band_rule(band).ceiling_mm, band),
    )


class StrategyRegistry:
    """Name to strategy, with derived graphs built once per graph version."""

    KNOWN = ("constraint_aware_direct", "hybrid", "structured_corridor")

    def __init__(self, graph: CorridorGraph) -> None:
        self.graph = graph
        self._cache: dict[str, RoutingStrategy] = {
            StructuredCorridorStrategy.name: StructuredCorridorStrategy()
        }

    @staticmethod
    def default(graph: CorridorGraph) -> "StrategyRegistry":
        return StrategyRegistry(graph)

    def get(self, name: str) -> RoutingStrategy:
        cached = self._cache.get(name)
        if cached is not None:
            return cached
        if name == ConstraintAwareDirectStrategy.name:
            built: RoutingStrategy = ConstraintAwareDirectStrategy(
                connector_lattice(self.graph, include_lanes=False)
            )
        elif name == HybridRoutingStrategy.name:
            built = HybridRoutingStrategy(connector_lattice(self.graph, include_lanes=True))
        else:
            raise PipelineConfigError(
                f"unknown routing strategy {name!r}; known {', '.join(self.KNOWN)}"
            )
        self._cache[name] = built
        return built

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self.KNOWN))

    def topology_digest(self, name: str) -> str:
        """Digest of the graph a strategy actually searches - proof it is not crippled."""
        return canonical_digest(
            [name, self.get(name).graph_for(self.graph).topology_digest()]
        )


def request_digest(request: PlanRequest) -> str:
    """The identity of one demand item, so a comparison can prove it used the same one."""
    return request.request_digest()


def identical_inputs_digest(
    requests: tuple[PlanRequest, ...], *, seed: int, cutoff_us: int
) -> str:
    """Hash of the shared demand, seed and cutoff a three-way comparison runs on."""
    return canonical_digest([[request_digest(item) for item in requests], seed, cutoff_us])


__all__ = [
    "DIRECT_PREFIX",
    "ConstraintAwareDirectStrategy",
    "HybridRoutingStrategy",
    "RoutingStrategy",
    "StrategyRegistry",
    "StructuredCorridorStrategy",
    "connector_lattice",
    "identical_inputs_digest",
    "request_digest",
]
