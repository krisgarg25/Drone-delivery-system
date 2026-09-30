"""Deterministic time and configuration-release identity.

`08-repo-bootstrap-checklist.md` section 10 requires a deterministic time helper test and a
stable scenario/config hash before merge. Time is integer microseconds; the release hash is
the identity every plan, decision and run must record.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from droneops_platform import Clock, FrozenClock, get_clock, set_clock
from droneops_sim_runner.hashing import collect_file_digests, config_release_sha256

REPO_ROOT = Path(__file__).resolve().parents[2]
MICROSECONDS = 1_000_000

pytestmark = pytest.mark.unit


def test_frozen_clock_never_moves_and_demands_a_timezone() -> None:
    fixed = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
    clock: Clock = FrozenClock(fixed)
    assert clock.now() == clock.now()
    assert clock.now().tzinfo is not None
    with pytest.raises(ValueError, match="timezone-aware"):
        FrozenClock(datetime(2026, 9, 30, 12, 0))


def test_global_clock_is_restored() -> None:
    original = get_clock()
    try:
        set_clock(FrozenClock(datetime(2026, 1, 1, tzinfo=UTC)))
        assert get_clock().now() == datetime(2026, 1, 1, tzinfo=UTC)
    finally:
        set_clock(original)
    assert get_clock() is original


def test_time_is_integer_microseconds() -> None:
    start = datetime(2026, 9, 30, 6, 0, tzinfo=UTC)
    end = datetime(2026, 9, 30, 6, 0, 2, 500_000, tzinfo=UTC)
    elapsed_us = int((end - start).total_seconds() * MICROSECONDS)
    assert elapsed_us == 2_500_000
    assert isinstance(elapsed_us, int)


def test_config_release_digest_is_canonical_and_content_sensitive(tmp_path: Path) -> None:
    source = tmp_path / "configs"
    shutil.copytree(REPO_ROOT / "configs", source)
    before = config_release_sha256(tmp_path)

    entries = collect_file_digests(tmp_path, ("configs",))
    assert entries, "the config release must cover the versioned safety inputs"
    # Collection is sorted by path, so the digest cannot depend on directory order.
    assert entries == sorted(entries, key=lambda entry: entry["path"])

    (source / "injected.yaml").write_text("authorized_for_flight: true\n", encoding="utf-8")
    assert config_release_sha256(tmp_path) != before


def test_config_release_digest_is_stable_across_calls() -> None:
    assert config_release_sha256(REPO_ROOT) == config_release_sha256(REPO_ROOT)
