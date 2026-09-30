"""Determinism: hash-seed independence, insertion-order independence, stable digests.

Candidate generation is run in a *fresh interpreter* under several
``PYTHONHASHSEED`` values, because an in-process test cannot prove anything about
dict or set iteration order - the parent interpreter already fixed its seed.

What this does not prove: that the run is reproducible across machines; that needs
the golden replay gate.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from droneops_corridor_graph import CostProfile, generate_candidates
from droneops_geom_core import Interval

from corridor_support import SYNTHETIC_BUNDLE, DirectoryBundleSource, build_square_graph

TESTS_DIR = Path(__file__).resolve().parent

PROBE = """
import sys, json
sys.path.insert(0, %r)
from corridor_support import build_square_graph, DirectoryBundleSource, SYNTHETIC_BUNDLE
from droneops_corridor_graph import CostProfile, generate_candidates, load_graph
from droneops_geom_core import Interval
import dataclasses

graph = build_square_graph()
profile = CostProfile(band_change_units=5, merge_penalty_units=1, crossing_penalty_units=2)
candidates = generate_candidates(
    graph, "source", "target", k=2, profile=profile, window=Interval(0, 10**9)
)
shuffled = dataclasses.replace(
    graph,
    edges=dict(reversed(list(graph.edges.items()))),
    nodes=dict(reversed(list(graph.nodes.items()))),
)
shuffled_candidates = generate_candidates(
    shuffled, "source", "target", k=2, profile=profile, window=Interval(0, 10**9)
)
bundle_graph = load_graph(DirectoryBundleSource(SYNTHETIC_BUNDLE))
epoch = bundle_graph.metadata.epoch_us
second = generate_candidates(
    bundle_graph, "hub-central-synthetic", "hub-north-synthetic", k=5,
    profile=CostProfile(band_change_units=200, merge_penalty_units=10, crossing_penalty_units=25),
    window=Interval(epoch, epoch + 60 * 60_000_000),
)
payload = {
    "square": [list(path.edge_ids) for path in candidates.candidates],
    "square_shuffled": [list(path.edge_ids) for path in shuffled_candidates.candidates],
    "square_ranking": candidates.ranking_digest(),
    "square_shuffled_ranking": shuffled_candidates.ranking_digest(),
    "bundle": [list(path.edge_ids) for path in second.candidates],
    "bundle_costs": [path.cost_units for path in second.candidates],
    "bundle_ranking": second.ranking_digest(),
    "topology": bundle_graph.topology_digest(),
    "merges": [merge.node_id for merge in bundle_graph.merge_nodes()],
    "crossings": [list(pair) for pair in bundle_graph.crossings()],
    "locks": list(bundle_graph.plan_lock_order(list(bundle_graph.edge_ids[:4]), 1_000, 5_000)),
}
print(json.dumps(payload, sort_keys=True))
""" % str(TESTS_DIR)


def run_probe(seed: str) -> dict[str, Any]:
    env = dict(os.environ)
    env["PYTHONHASHSEED"] = seed
    completed = subprocess.run(
        [sys.executable, "-c", PROBE],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        cwd=str(TESTS_DIR.parent),
    )
    assert completed.returncode == 0, completed.stderr
    parsed = json.loads(completed.stdout)
    assert isinstance(parsed, dict)
    return parsed


def test_candidate_order_is_identical_under_every_hash_seed() -> None:
    """Changing ``PYTHONHASHSEED`` reorders nothing - the replay gate depends on it."""
    baseline = run_probe("0")
    for seed in ("1", "7", "12345"):
        observed = run_probe(seed)
        assert observed == baseline, f"output changed under PYTHONHASHSEED={seed}"


def test_insertion_order_of_the_source_dicts_does_not_change_the_answer() -> None:
    """Equal-cost detours separate on lane id, so reversing dict order is a no-op."""
    probe = run_probe("3")
    assert probe["square"] == probe["square_shuffled"]
    assert probe["square_ranking"] == probe["square_shuffled_ranking"]


def test_lock_acquisition_order_is_canonical(synthetic_graph: Any) -> None:
    """Cells are locked in geom-core's documented order, so two plans cannot deadlock."""
    locks = run_probe("0")["locks"]
    assert locks == sorted(locks)
    assert all(lock.startswith("volume:") for lock in locks)


def test_identical_input_produces_a_byte_identical_ranking(synthetic_graph: Any) -> None:
    """Same graph, same profile, same window: the ranking bytes never move."""
    epoch = synthetic_graph.metadata.epoch_us
    horizon = Interval(epoch, epoch + 60 * 60_000_000)
    profile = CostProfile(band_change_units=200, merge_penalty_units=10, crossing_penalty_units=25)
    digests = []
    for _ in range(4):
        candidates = generate_candidates(
            synthetic_graph,
            "hub-central-synthetic",
            "node-sparesdepot-synthetic",
            k=5,
            profile=profile,
            window=horizon,
        )
        payload = json.dumps(
            [
                [list(path.edge_ids), path.cost_units, path.digest()]
                for path in candidates.candidates
            ],
            sort_keys=True,
        ).encode("utf-8")
        digests.append(hashlib.sha256(payload).hexdigest())
    assert len(set(digests)) == 1
    assert digests[0] == digests[-1]


def test_square_graph_helper_and_bundle_agree_on_costs(synthetic_graph: Any) -> None:
    """Cost is declared length in metres plus penalties, in integers, always."""
    square = build_square_graph()
    candidates = generate_candidates(
        square, "source", "target", k=1, profile=CostProfile(), window=Interval(0, 10**9)
    )
    assert candidates.candidates[0].cost_units == 8
    assert candidates.candidates[0].length_mm == 8000
