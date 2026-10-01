"""Phase 3 performance gates: the CPU-bound half of `scripts/phase3_bench.py` in CI.

`technical-build/06` section 11 asks for measurement, not assumption, and Phase 3's exit
criterion says performance is *profiled and documented with machine details*. The full
workload list needs the live PostGIS container and several minutes to run, so it lives in
the committed harness (`scripts/phase3_bench.py`, `scripts/phase3_reservation_scale.sql`)
and its numbers are recorded in
`docs/performance/phase-3-performance-report.md`.

What runs here is the part that is deterministic, cheap and regression-prone: the planner,
the batch scenario kernel and replay. The ceilings are tripwires, not claims: each one is
roughly ten times the median measured on the machine named in the report, so a real
regression is caught while a slower CI box does not fail for being a slower CI box. No
safety assertion lives in this file, and nothing here scales a number down to pass.

Run the whole thing yourself:

    uv run --no-sync python scripts/phase3_bench.py --all
    cat scripts/phase3_reservation_scale.sql | docker exec -i droneops-local-postgres-1 \\
        psql -U droneops -d droneops
"""

from __future__ import annotations

import importlib.util
import statistics
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "phase3_bench.py"

pytestmark = pytest.mark.unit


def _bench():
    """Load the benchmark harness as a module: one implementation, two consumers."""
    spec = importlib.util.spec_from_file_location("phase3_bench", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["phase3_bench"] = module
    spec.loader.exec_module(module)
    return module


def test_percentile_matches_a_known_distribution() -> None:
    """The reporting maths is checked before any reported number is trusted."""
    bench = _bench()
    values = [float(index) for index in range(1, 101)]
    assert bench.percentile(values, 50) == pytest.approx(50.5)
    assert bench.percentile(values, 95) == pytest.approx(95.05)
    assert bench.percentile(values, 99) == pytest.approx(99.01)
    assert bench.percentile([1.0], 99) == 1.0
    assert bench.percentile([], 50) != bench.percentile([], 50)  # nan, never a silent zero


def test_planner_candidate_sweep_stays_inside_the_tripwire() -> None:
    """40 demands on one corridor: every one returns, refusals are cheap, plans are not slow.

    Measured median for a demand that produced a selected plan was about 49 ms on the
    reference machine, so 500 ms is a regression tripwire with an order of headroom rather
    than a performance promise.
    """
    bench = _bench()
    samples, extra = bench.bench_planner_candidates(40)
    assert len(samples) == 40, "every demand must be timed, including the refused ones"
    assert extra["planner_exceptions"] == 0
    statuses = extra["selected_statuses"]
    assert sum(statuses.values()) == 40
    accepted = extra["per_status_median_ms"].get("ok")
    if accepted is not None:
        # `per_status_median_ms` is already milliseconds, hence 500 and not 0.5.
        assert accepted < 500.0, f"planner regression: accepted-plan median {accepted} ms"


def test_batch_scenarios_are_fast_and_deterministic() -> None:
    """Two seeds, twice each: the kernel's throughput and its reproducibility gate."""
    bench = _bench()
    samples, extra = bench.bench_batch_scenarios(2, "rush")
    assert extra["scenario_failures"] == 0
    assert len(samples) == 2
    assert statistics.median(samples) < 2.0, (
        "scenario replay got an order of magnitude slower than the measured 0.11 s")
    keys = set(extra["metrics_keys"])
    assert {"missions_completed", "missions_late", "energy_total_wh",
            "repositioning_energy_wh", "invalid_committed_overlaps"} <= keys
    rows = extra["rows"]
    for row in rows:
        assert row["invalid_overlaps"] == 0, (
            "I1: a committed overlap in the batch run is a release blocker, not a warning")

    # Same seed, twice, same answer: the I7 half of the replay gate.
    again, extra_again = bench.bench_batch_scenarios(1, "rush")
    assert again and extra_again["rows"] == [rows[0]], (
        "I7: the same seed produced different metrics on the second run")


def test_replay_is_identical_and_cheap() -> None:
    """Byte-identical replay, and the cost CI actually pays per push.

    Measured on the reference machine: the kernel run itself is about 10 ms, and the whole
    `droneops-sim replay` command - interpreter start, imports, run, hash comparison - is
    about 0.3 s, so the gate is dominated by process startup rather than by the simulation.
    """
    bench = _bench()
    samples, extra = bench.bench_replay_cost(3)
    assert extra["identical_outputs"] is True, "I7: same seed must produce identical output"
    assert extra["distinct_output_digests"] == 1
    assert set(extra["ci_replay_return_codes"]) == {0}, "the replay gate must pass"
    assert extra["ci_replay_median_seconds"] < 10.0
    assert len(samples) == extra["in_process_runs"] + extra["ci_replay_runs"], (
        "the sample list must cover both the in-process runs and the subprocess runs")
