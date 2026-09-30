"""Fixtures for the planner: the frozen bundle, the policy, the weather trace."""

from __future__ import annotations

import pytest

from droneops_corridor_graph import CorridorGraph
from droneops_planner import PolicySnapshot
from planner_support import (
    bundle_policy,
    load_bundle_graph,
    wind_trace_steps,
)

__all__ = ["graph", "epoch_us", "policy", "wind_steps"]


@pytest.fixture(scope="session")
def graph() -> CorridorGraph:
    return load_bundle_graph()


@pytest.fixture(scope="session")
def epoch_us(graph: CorridorGraph) -> int:
    return graph.metadata.epoch_us


@pytest.fixture(scope="session")
def wind_steps() -> tuple[tuple[int, int, int], ...]:
    return wind_trace_steps()


@pytest.fixture
def policy() -> PolicySnapshot:
    return bundle_policy()
