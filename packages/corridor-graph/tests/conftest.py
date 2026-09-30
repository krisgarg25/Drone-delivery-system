"""Fixtures for corridor-graph: the real bundles, read through the injected source."""

from __future__ import annotations

import pytest

from corridor_support import BASIC_BUNDLE, SYNTHETIC_BUNDLE, DirectoryBundleSource
from droneops_corridor_graph import CorridorGraph, CostProfile, load_graph

__all__ = ["BASIC_BUNDLE", "SYNTHETIC_BUNDLE", "DirectoryBundleSource", "profile"]


@pytest.fixture(scope="session")
def synthetic_bundle() -> DirectoryBundleSource:
    return DirectoryBundleSource(SYNTHETIC_BUNDLE)


@pytest.fixture(scope="session")
def basic_bundle() -> DirectoryBundleSource:
    return DirectoryBundleSource(BASIC_BUNDLE)


@pytest.fixture(scope="session")
def synthetic_graph(synthetic_bundle: DirectoryBundleSource) -> CorridorGraph:
    return load_graph(synthetic_bundle)


@pytest.fixture(scope="session")
def basic_graph(basic_bundle: DirectoryBundleSource) -> CorridorGraph:
    return load_graph(basic_bundle)


@pytest.fixture
def profile() -> CostProfile:
    return CostProfile(band_change_units=200, merge_penalty_units=10, crossing_penalty_units=25)
