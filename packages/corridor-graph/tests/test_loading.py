"""The topology is read from the bundle: counts, provenance, identity and refusals.

What this does not prove: anything about real Chandigarh lanes. It proves the graph
follows whichever bundle it is given.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from droneops_corridor_graph import (
    BundleIntegrityError,
    GraphLoadError,
    LaneDirection,
    TopologyError,
    load_graph,
    sha256_text,
)
from droneops_geom_core import ResourceKind

from corridor_support import (
    BASIC_BUNDLE,
    SYNTHETIC_BUNDLE,
    DirectoryBundleSource,
    copy_bundle,
    edit_document,
)


def declared_counts(directory: Path) -> dict[str, Any]:
    document = yaml.safe_load((directory / "corridors.yaml").read_text(encoding="utf-8"))
    facilities = yaml.safe_load((directory / "facilities.yaml").read_text(encoding="utf-8"))
    return {
        "edges": len(document["edges"]),
        "bands": len(document["bands"]),
        "closures": len(document.get("closures") or []),
        "nodes": len(facilities["facilities"]),
        "lengths_m": {edge["corridor_id"]: edge["length_m"] for edge in document["edges"]},
        "endpoints": {(edge["from"], edge["to"]) for edge in document["edges"]},
    }


@pytest.mark.parametrize("directory", [SYNTHETIC_BUNDLE, BASIC_BUNDLE], ids=["synthetic", "basic"])
def test_graph_matches_its_own_bundle_document(directory: Path) -> None:
    """Every node, lane and length in the graph is present in the YAML and nowhere else."""
    graph = load_graph(DirectoryBundleSource(directory))
    counts = declared_counts(directory)
    assert len(graph.edges) == counts["edges"]
    assert len(graph.nodes) == counts["nodes"]
    assert len(graph.bands) == counts["bands"]
    assert len(graph.overlay.restrictions) == counts["closures"]
    for edge in graph.edges.values():
        assert edge.length_mm == counts["lengths_m"][edge.edge_id] * 1000
        assert (edge.from_node, edge.to_node) in counts["endpoints"]


def test_provenance_flags_cannot_be_stripped(synthetic_graph: Any) -> None:
    """Section 3: a synthetic lane says so, and never claims safety."""
    assert synthetic_graph.metadata.authoritative is False
    assert synthetic_graph.metadata.safety_claim is False
    assert all(edge.synthetic for edge in synthetic_graph.edges.values())
    assert all(node.authoritative is False for node in synthetic_graph.nodes.values())


def test_lane_direction_is_derived_not_declared(
    synthetic_graph: Any, basic_graph: Any
) -> None:
    """basic pairs reverse lanes into one bidirectional group; synthetic does not."""
    assert synthetic_graph.lane_directions() == {"one_way": 26}
    assert basic_graph.lane_directions() == {"bidirectional": 22}
    groups = {edge.lane_group_id for edge in basic_graph.edges.values()}
    assert len(groups) == 11


def test_graph_version_tracks_content(synthetic_bundle: Any, basic_bundle: Any) -> None:
    first = load_graph(synthetic_bundle)
    again = load_graph(synthetic_bundle)
    assert first.graph_version == again.graph_version
    assert first.graph_version != load_graph(basic_bundle).graph_version


def test_bundle_hash_matches_manifest_digests(synthetic_graph: Any) -> None:
    """The manifest's per-file hashes are the frozen input identity (section 8)."""
    assert set(synthetic_graph.metadata.documents) == {
        "corridors.yaml",
        "demand.yaml",
        "facilities.yaml",
        "faults.yaml",
        "fleet.yaml",
        "policies.yaml",
        "scenario.yaml",
        "weather.yaml",
    }
    assert synthetic_graph.bundle_sha256.startswith("sha256:")


def test_edited_document_is_refused(tmp_path: Path) -> None:
    """A bundle whose file no longer matches its manifest cannot be loaded quietly."""
    directory = copy_bundle(tmp_path, SYNTHETIC_BUNDLE)
    lanes = yaml.safe_load((directory / "corridors.yaml").read_text(encoding="utf-8"))
    lanes["edges"][0]["length_m"] = 9999
    (directory / "corridors.yaml").write_text(yaml.safe_dump(lanes), encoding="utf-8")
    with pytest.raises(BundleIntegrityError, match="corridors.yaml hashes to"):
        load_graph(DirectoryBundleSource(directory))


def test_expired_bundle_fails_closed(tmp_path: Path) -> None:
    """Section 7 of the architecture: an expired restriction snapshot is unusable data."""
    directory = copy_bundle(tmp_path, SYNTHETIC_BUNDLE)
    edit_document(
        directory,
        "bundle.yaml",
        lambda document: document["source_manifest"]["restrictions"].__setitem__(
            "expires_at", "2000-01-01T00:00:00Z"
        ),
    )
    with pytest.raises(GraphLoadError, match="expired"):
        load_graph(DirectoryBundleSource(directory), as_of_us=10**15)
    loaded = load_graph(DirectoryBundleSource(directory))
    assert loaded.metadata.expires_at_us < loaded.metadata.epoch_us


def test_unknown_lane_endpoint_is_a_topology_defect(tmp_path: Path) -> None:
    directory = copy_bundle(tmp_path, BASIC_BUNDLE)
    edit_document(
        directory, "corridors.yaml", lambda document: document["edges"][0].__setitem__("to", "nowhere")
    )
    with pytest.raises(TopologyError, match="does not declare"):
        load_graph(DirectoryBundleSource(directory))


def test_unknown_band_on_a_lane_is_a_topology_defect(tmp_path: Path) -> None:
    directory = copy_bundle(tmp_path, BASIC_BUNDLE)
    edit_document(
        directory, "corridors.yaml", lambda document: document["edges"][0].__setitem__("band", "Z")
    )
    with pytest.raises(TopologyError, match="no band rule defines"):
        load_graph(DirectoryBundleSource(directory))


def test_missing_policies_epoch_is_refused(tmp_path: Path) -> None:
    source_dir = tmp_path / "bundle"
    source_dir.mkdir()
    (source_dir / "bundle.yaml").write_text(
        yaml.safe_dump(
            {"scenario_id": "x", "schema_version": 1, "seed": 1, "files": {"policies.yaml": "0" * 64}}
        ),
        encoding="utf-8",
    )
    (source_dir / "policies.yaml").write_text(yaml.safe_dump({"schema_version": 1}), encoding="utf-8")
    with pytest.raises(BundleIntegrityError):
        load_graph(DirectoryBundleSource(source_dir))


def test_resource_keys_use_the_canonical_geom_core_scheme(synthetic_graph: Any) -> None:
    """Section 7: identity comes from geom-core, so the ledger cannot respell a lane."""
    edge_id = synthetic_graph.edge_ids[0]
    corridor_key = synthetic_graph.edge_resource_key(edge_id)
    assert corridor_key.kind is ResourceKind.CORRIDOR
    assert corridor_key.canonical.startswith("corridor:lane-hcentral-hnorth-B-synthetic")
    cells = synthetic_graph.edge_volume_cell_keys(edge_id, 1_000, 5_000)
    assert cells and all(key.kind is ResourceKind.VOLUME_CELL for key in cells)
    assert {key.canonical.split(":")[2] for key in cells} == {"1000..5000"}
    locks = synthetic_graph.plan_lock_order(synthetic_graph.edge_ids[:3], 1_000, 5_000)
    assert list(locks) == sorted(locks)


def test_adjacency_and_crossing_queries_are_ordered(synthetic_graph: Any) -> None:
    adjacency = synthetic_graph.adjacency()
    assert list(adjacency) == sorted(adjacency)
    for node_id, neighbours in adjacency.items():
        assert neighbours == tuple(sorted(neighbours))
        assert all(to_node in synthetic_graph.nodes for to_node, _ in neighbours)
        assert [edge_id for _, edge_id in neighbours] == [
            edge.edge_id for edge in synthetic_graph.out_edges(node_id)
        ]
    crossings = synthetic_graph.crossings()
    assert crossings == tuple(sorted(set(crossings)))
    assert all(low != high for low, high in crossings)
    for low, high in crossings[:5]:
        assert high in synthetic_graph.crossing_partners(low)
        assert low in synthetic_graph.crossing_partners(high)


def test_merge_nodes_are_derived_from_degree(synthetic_graph: Any) -> None:
    merges = synthetic_graph.merge_nodes()
    assert [merge.node_id for merge in merges] == sorted(merge.node_id for merge in merges)
    for merge in merges:
        assert merge.kind.value in {"convergence", "divergence", "intersection"}
        assert merge.inbound and merge.outbound
        for arriving, departing in merge.conflict_pairs:
            assert arriving != departing
            assert synthetic_graph.edges[arriving].lane_group_id != (
                synthetic_graph.edges[departing].lane_group_id
            )


def test_alternative_bundle_directory_produces_alternative_topology() -> None:
    """The graph is a function of the bytes: different bundle, different topology."""
    synthetic = load_graph(DirectoryBundleSource(SYNTHETIC_BUNDLE))
    basic = load_graph(DirectoryBundleSource(BASIC_BUNDLE))
    assert synthetic.topology_digest() != basic.topology_digest()
    assert set(synthetic.node_ids) & set(basic.node_ids) == set()
