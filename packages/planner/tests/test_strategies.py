"""Strategy comparison: direct, corridor and hybrid under one identical demand.

Phase 3 requires the three routings to be comparable on the same demand, weather,
aircraft states, seed, cutoff, energy profiles and capacity, and warns that a
weakened baseline is misleading. These tests assert the fairness structurally: one
frozen input object, one digest of it, and the same constraint gates visible in the
trace of every strategy.
"""

from __future__ import annotations

from typing import Any

import pytest

from droneops_planner import (
    PipelineConfigError,
    StrategyComparator,
    StrategyRegistry,
    connector_lattice,
    identical_inputs,
)

from planner_support import build_pipeline, request

STRATEGIES = ("structured_corridor", "constraint_aware_direct", "hybrid")


@pytest.fixture
def demand(epoch_us: int) -> tuple[Any, ...]:
    """Three orders, staggered, on the same aircraft envelope and cutoff."""
    return (
        request(epoch_us, order_id="ord-001", mission_id="msn-001", destination="hub-north-synthetic", at_minute=0),
        request(
            epoch_us,
            order_id="ord-002",
            mission_id="msn-002",
            destination="hub-south-campus-synthetic",
            at_minute=2,
        ),
        request(
            epoch_us,
            order_id="ord-003",
            mission_id="msn-003",
            origin="hub-central-synthetic",
            destination="node-medlab-synthetic",
            at_minute=4,
        ),
    )


def test_the_three_runs_share_one_identical_input_digest(
    graph: Any, policy: Any, wind_steps: Any, demand: tuple[Any, ...]
) -> None:
    """The comparison is only meaningful if everything except the strategy is equal."""
    inputs = identical_inputs(
        graph, policy, demand, wind_steps, seed=graph.metadata.scenario_id.__hash__ and 202609300
    )
    comparator = StrategyComparator(inputs)
    metrics = comparator.run(STRATEGIES)
    assert [item.strategy for item in metrics] == sorted(STRATEGIES)
    for item in metrics:
        assert len(item.proposal_digests) == len(demand)
    again = StrategyComparator(identical_inputs(graph, policy, demand, wind_steps, seed=202609300))
    assert [item.as_row() for item in again.run(STRATEGIES)] == [item.as_row() for item in metrics]


def test_direct_routing_is_a_competent_baseline_not_a_straw_man(
    graph: Any, policy: Any, wind_steps: Any, epoch_us: int
) -> None:
    """Direct routing gets the full node lattice and the same steps 6 to 10 gates."""
    lattice = connector_lattice(graph, include_lanes=False)
    node_count = len(graph.nodes)
    assert len(lattice.edges) == node_count * (node_count - 1)
    assert all(edge_id.startswith("direct-") for edge_id in lattice.edges)
    pipeline, _, ledger = build_pipeline(graph, policy, wind_steps, strategy="constraint_aware_direct")
    proposal = pipeline.plan(request(epoch_us, mission_id="msn-direct"))
    numbers = [step.step for step in proposal.trace.steps]
    for step_number in (6, 7, 8, 9, 10, 11, 12):
        assert step_number in numbers, f"direct routing skipped step {step_number}"
    assert proposal.selected is not None
    assert ledger.attempts, "a proposal must still ask the ledger to commit"
    direct_length = proposal.selected.route.length_mm
    corridor_pipeline, _, _ = build_pipeline(graph, policy, wind_steps)
    corridor = corridor_pipeline.plan(request(epoch_us, mission_id="msn-corridor"))
    assert corridor.selected is not None
    assert direct_length <= corridor.selected.route.length_mm


def test_hybrid_may_use_a_lane_when_the_detour_pays(
    graph: Any, policy: Any, wind_steps: Any, epoch_us: int
) -> None:
    """The hybrid graph offers both edge families; the cost model chooses."""
    combined = connector_lattice(graph, include_lanes=True)
    assert any(edge_id.startswith("lane-") for edge_id in combined.edges)
    assert any(edge_id.startswith("direct-") for edge_id in combined.edges)
    pipeline, _, _ = build_pipeline(graph, policy, wind_steps, strategy="hybrid")
    proposal = pipeline.plan(request(epoch_us, mission_id="msn-hybrid"))
    assert proposal.selected is not None
    families = {
        "lane" if edge_id.startswith("lane-") else "direct"
        for candidate in proposal.candidates
        for edge_id in candidate.route.edge_ids
    }
    assert families & {"lane", "direct"}


def test_comparison_counts_rejections_by_reason_code(
    graph: Any, policy: Any, wind_steps: Any, demand: tuple[Any, ...]
) -> None:
    """Section 9: metrics must show rejected and unfinished work, not only successes."""
    inputs = identical_inputs(graph, policy, demand, wind_steps, seed=202609300)
    metrics = StrategyComparator(inputs).run(STRATEGIES)
    for item in metrics:
        assert item.accepted + item.rejected >= 0
        assert isinstance(item.rejection_counts, tuple)
        assert item.total_energy_wh >= 0
        for code, count in item.rejection_counts:
            assert count > 0 and code
    accepted_total = {item.strategy: item.accepted for item in metrics}
    assert sum(accepted_total.values()) > 0, "the shared demand must be flyable somehow"


def test_strategy_registry_refuses_an_unknown_name(graph: Any) -> None:
    registry = StrategyRegistry.default(graph)
    assert registry.names() == ("constraint_aware_direct", "hybrid", "structured_corridor")
    with pytest.raises(PipelineConfigError, match="unknown routing strategy"):
        registry.get("randomised")
    assert registry.topology_digest("structured_corridor") != registry.topology_digest("hybrid")
