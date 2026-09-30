"""Test support for corridor-graph: bundle reading and hand-built graphs.

Lives in its own module (rather than in ``conftest``) because several packages in
this workspace have a ``tests/conftest.py``, and a test module that imports a file
by the name ``conftest`` will get whichever one was imported first.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from droneops_corridor_graph import (
    AltitudeBandRule,
    ClosureOverlay,
    CorridorEdge,
    CorridorGraph,
    CorridorNode,
    GeometryPoint,
    GraphSourceMetadata,
    LaneDirection,
    LaneRule,
    NodeKind,
    Restriction,
    RestrictionKind,
    minutes_to_us,
    sha256_text,
)
from droneops_corridor_graph.geometry import swept_cells
from droneops_geom_core import AltitudeReference, GeoPoint, LocalPlane

REPO_ROOT = Path(__file__).resolve().parents[3]
SCENARIO_ROOT = REPO_ROOT / "simulation" / "scenarios"
SYNTHETIC_BUNDLE = SCENARIO_ROOT / "chandigarh-synthetic-v1"
BASIC_BUNDLE = SCENARIO_ROOT / "chandigarh-basic"


class DirectoryBundleSource:
    """A ``BundleSource`` over one scenario directory on disk."""

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory or SYNTHETIC_BUNDLE
        self._cache: dict[str, Mapping[str, Any]] = {}

    def text(self, name: str) -> str:
        return (self.directory / name).read_text(encoding="utf-8")

    def document(self, name: str) -> Mapping[str, Any]:
        if name not in self._cache:
            parsed = yaml.safe_load(self.text(name))
            if not isinstance(parsed, Mapping):
                raise ValueError(f"{name} is not a mapping")
            self._cache[name] = parsed
        return self._cache[name]


def closure(edge_id: str, from_minute: int, to_minute: int, epoch_us: int = 0) -> Restriction:
    """A closure written in bundle minutes, absolute at load like the real overlay."""
    return Restriction(
        restriction_id=f"closure-{edge_id}-{from_minute}-{to_minute}",
        edge_id=edge_id,
        kind=RestrictionKind.CLOSED,
        start_us=minutes_to_us(from_minute, epoch_us),
        end_us=minutes_to_us(to_minute, epoch_us),
        human_reason="synthetic survey overlay",
        technical_detail=f"{edge_id} closed minutes {from_minute}..{to_minute}",
    )


def overlay_with(*restrictions: Restriction) -> ClosureOverlay:
    return ClosureOverlay(tuple(restrictions))


def copy_bundle(tmp_path: Path, source: Path) -> Path:
    """Materialise a real bundle into ``tmp_path`` so a test can edit one document."""
    target = tmp_path / "bundle"
    target.mkdir(parents=True, exist_ok=True)
    for path in sorted(source.glob("*.yaml")):
        (target / path.name).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    return target


def refresh_manifest(directory: Path) -> None:
    """Re-declare every file hash: integrity passes, so only the edit is visible."""
    manifest_path = directory / "bundle.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    files = manifest["files"]
    for name in sorted(files):
        files[name] = sha256_text((directory / name).read_text(encoding="utf-8"))
    manifest_path.write_text(yaml.safe_dump(manifest), encoding="utf-8")


def edit_document(directory: Path, name: str, mutate: Any) -> None:
    """Rewrite one bundle document through a mapping mutation, keeping hashes honest."""
    path = directory / name
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    mutate(document)
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    refresh_manifest(directory)


def build_square_graph(
    *,
    overlay: ClosureOverlay | None = None,
    length_mm: int = 4000,
    east_band: str = "B",
) -> CorridorGraph:
    """A hand-built diamond: s->a->t and s->b->t, one lane each direction.

    Both detours cost exactly the same, so the *only* thing that can order them is
    the documented tie-break. Node ids are chosen so alphabetical order
    (``mid-a`` before ``mid-b``) differs from insertion order, which is what a set
    or dict iteration would otherwise leak.
    """
    reference = AltitudeReference.AGL
    positions = {
        "source": GeoPoint(30.7383, 76.7936),
        "mid-b": GeoPoint(30.7500, 76.8000),
        "mid-a": GeoPoint(30.7500, 76.7870),
        "target": GeoPoint(30.7660, 76.7850),
    }
    nodes = {
        node_id: CorridorNode(
            node_id=node_id,
            position=position,
            ground_altitude_mm=320_000,
            reference=reference,
            kind=NodeKind.NODE if node_id.startswith("mid") else NodeKind.HUB,
        )
        for node_id, position in positions.items()
    }
    bands = {
        band: AltitudeBandRule(
            band=band,
            floor_mm=index * 20_000,
            ceiling_mm=(index + 1) * 20_000,
            speed_cap_mm_per_s=15_000,
            reference=reference,
        )
        for index, band in enumerate(("A", "B", "C"))
    }
    edges: dict[str, CorridorEdge] = {}

    def lane(edge_id: str, start: str, goal: str, band: str) -> None:
        edges[edge_id] = CorridorEdge(
            edge_id=edge_id,
            from_node=start,
            to_node=goal,
            lane_group_id=edge_id,
            lane_direction=LaneDirection.ONE_WAY,
            band=band,
            length_mm=length_mm,
            rule=LaneRule(
                speed_cap_mm_per_s=15_000,
                max_wind_mm_per_s=11_000,
                width_mm=100_000,
                band=band,
            ),
            geometry=(
                GeometryPoint(point=positions[start], altitude_mm=60_000, reference=reference),
                GeometryPoint(point=positions[goal], altitude_mm=60_000, reference=reference),
            ),
            reference=reference,
        )

    lane("lane-s-mid-a", "source", "mid-a", east_band)
    lane("lane-mid-a-t", "mid-a", "target", east_band)
    lane("lane-s-mid-b", "source", "mid-b", east_band)
    lane("lane-mid-b-t", "mid-b", "target", east_band)
    plane = LocalPlane(positions["source"])
    metadata = GraphSourceMetadata(
        scenario_id="synthetic-square-test",
        schema_version=1,
        bundle_sha256="sha256:" + "0" * 64,
        graph_version="corridor-graph-v1:test",
        documents={"corridors.yaml": "0" * 64},
        altitude_reference=reference,
        source="test fixture",
        epoch_us=0,
    )
    return CorridorGraph(
        metadata=metadata,
        nodes=nodes,
        edges=edges,
        bands=bands,
        overlay=overlay or ClosureOverlay.empty(),
        plane=plane,
        edge_cells={edge_id: swept_cells(edge, plane) for edge_id, edge in edges.items()},
        scenario_title="synthetic square",
    )


def build_square_with_pads(
    *,
    loading_us: int = 2 * 60 * 1_000_000,
    offload_us: int = 60_000_000,
) -> CorridorGraph:
    """The same diamond, but with the facility capacity a planner pipeline needs."""
    from droneops_corridor_graph import FacilityAttributes

    graph = build_square_graph()
    nodes = {}
    for node_id, node in graph.nodes.items():
        nodes[node_id] = CorridorNode(
            node_id=node_id,
            position=node.position,
            ground_altitude_mm=node.ground_altitude_mm,
            reference=node.reference,
            kind=node.kind,
            facility=FacilityAttributes(
                facility_id=node_id,
                kind="hub",
                pads=2,
                charger_bays=1,
                open_us=0,
                close_us=60 * 60 * 1_000_000,
                loading_turnaround_us=loading_us,
                offload_turnaround_us=offload_us,
            ),
        )
    return CorridorGraph(
        metadata=graph.metadata,
        nodes=nodes,
        edges=graph.edges,
        bands=graph.bands,
        overlay=graph.overlay,
        plane=graph.plane,
        edge_cells=graph.edge_cells,
        scenario_title=graph.scenario_title,
    )
