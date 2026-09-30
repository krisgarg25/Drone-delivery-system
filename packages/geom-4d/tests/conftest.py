"""Fixtures for the geom-4d suite: the real policy, a grid, a body.

The scenario *builders* live in :mod:`geom4d_scenarios`, not here, because a
``from conftest import ...`` collides with the repository root ``conftest`` module
once the whole monorepo suite runs in one process.
"""

from __future__ import annotations

import pytest
from geom4d_scenarios import REPO_ROOT

from droneops_geom_4d import (
    DEFAULT_POLICY_RELPATH,
    ProtectedBody,
    ProtectedVolumePolicy,
    load_policy_file,
)
from droneops_geom_core import CHANDIGARH_UTM_EPSG, GridSpec, metres_to_millimetres


@pytest.fixture(scope="session")
def policy() -> ProtectedVolumePolicy:
    """The repository's protected-volume policy, read the way production reads it."""
    return load_policy_file(REPO_ROOT / DEFAULT_POLICY_RELPATH)


@pytest.fixture(scope="session")
def body() -> ProtectedBody:
    """A small delivery UAS as a vertical cylinder: 0.15 m radius, 0.2 m tall."""
    return ProtectedBody.of(0.15, 0.1)


@pytest.fixture(scope="session")
def grid() -> GridSpec:
    """A corridor grid: 50 m cells, 10 m vertical bands, the pinned Chandigarh CRS."""
    return GridSpec(
        epsg=CHANDIGARH_UTM_EPSG,
        cell_size_mm=metres_to_millimetres(50.0),
        vertical_band_mm=metres_to_millimetres(10.0),
    )
