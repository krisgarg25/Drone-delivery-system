"""corridor-graph: the versioned directed lane graph and candidate route generation.

Owns lane graph, candidate paths, altitude band rules, merge nodes and closure /
restriction overlays (``docs/conventions.md`` section 7). It owns no live vehicle
API, no persistence, no swept 4D volume and no reservation authority: a graph can
say which lanes exist and which routes are cheap, never that a flight is safe.

Design notes worth keeping in mind while editing:

* the topology is loaded from a frozen scenario bundle through an injected
  :class:`~droneops_corridor_graph.loading.BundleSource`, so this package declares
  no YAML parser and no filesystem dependency, and a bundle that no longer matches
  its manifest is refused rather than quietly re-read;
* ``geom-4d`` is **not** imported. It is being built in parallel and owns swept
  volumes; the graph exposes the volume cells a declared centreline passes through
  and the planner asks an injected port for the precise sweep;
* every sequence is emitted in a documented total order - cost, then node id, then
  edge id - because an unsorted dict or set would make ``PYTHONHASHSEED`` an
  input to the answer.
"""

from __future__ import annotations

from .errors import (
    AltitudeBandError,
    BundleIntegrityError,
    ClosureOverlayError,
    CorridorGraphError,
    GraphLoadError,
    PathGenerationError,
    StaleSourceError,
    TopologyError,
)
from .geometry import DEFAULT_GRID, PlanarSegment, plane_for, swept_cells
from .graph import CorridorGraph, OverlayView
from .kshortest import (
    MM_PER_COST_UNIT,
    CandidateSet,
    CorridorPath,
    CostProfile,
    generate_candidates,
)
from .loading import (
    BundleManifest,
    BundleSource,
    load_graph,
    load_manifest,
    sha256_text,
)
from .model import (
    AltitudeBandRule,
    CorridorEdge,
    CorridorNode,
    FacilityAttributes,
    GeometryPoint,
    GraphSourceMetadata,
    LaneDirection,
    LaneRule,
    MergeKind,
    MergeNode,
    NodeKind,
    canonical_digest,
    minutes_to_us,
)
from .overlay import ClosureOverlay, Restriction, RestrictionKind, overlay_from_document

__all__ = [
    "DEFAULT_GRID",
    "MM_PER_COST_UNIT",
    "AltitudeBandError",
    "AltitudeBandRule",
    "BundleIntegrityError",
    "BundleManifest",
    "BundleSource",
    "CandidateSet",
    "ClosureOverlay",
    "ClosureOverlayError",
    "CorridorEdge",
    "CorridorGraph",
    "CorridorGraphError",
    "CorridorNode",
    "CorridorPath",
    "CostProfile",
    "FacilityAttributes",
    "GeometryPoint",
    "GraphLoadError",
    "GraphSourceMetadata",
    "LaneDirection",
    "LaneRule",
    "MergeKind",
    "MergeNode",
    "NodeKind",
    "OverlayView",
    "PathGenerationError",
    "PlanarSegment",
    "Restriction",
    "RestrictionKind",
    "StaleSourceError",
    "TopologyError",
    "canonical_digest",
    "generate_candidates",
    "load_graph",
    "load_manifest",
    "minutes_to_us",
    "overlay_from_document",
    "plane_for",
    "sha256_text",
    "swept_cells",
]
