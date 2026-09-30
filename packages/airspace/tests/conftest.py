"""Fixtures for the airspace package: a fixed clock and the trust policy."""

from __future__ import annotations

from datetime import datetime

import pytest
from airspace_support import NOW, FreshnessPolicy, policy

__all__ = ["NOW", "now", "freshness_policy"]


@pytest.fixture
def now() -> datetime:
    return NOW


@pytest.fixture
def freshness_policy() -> FreshnessPolicy:
    return policy()
