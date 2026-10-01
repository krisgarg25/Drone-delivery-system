"""Pytest fixtures for the gis-etl tests.

Builders and constants live in ``gis_etl_support`` so that no test module has to
``from conftest import ...`` - that import resolves to whichever conftest was loaded
first across the whole repository, and it silently gave these tests the concurrency
fixtures instead.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from droneops_adapters_gis_etl.cache import RawCache
from droneops_adapters_gis_etl.config import GisConfig
from gis_etl_support import SleepRecorder, load_test_config, write_config_root


@pytest.fixture
def config_root(tmp_path: Path) -> Path:
    return write_config_root(tmp_path)


@pytest.fixture
def config(config_root: Path) -> GisConfig:
    return load_test_config(config_root)


@pytest.fixture
def cache(config: GisConfig) -> RawCache:
    return RawCache(config.raw_dir / "overpass", namespace="")


@pytest.fixture
def sleeps() -> Iterator[SleepRecorder]:
    yield SleepRecorder()
