"""Seeded comparison: direct, lane-based and hybrid routing under identical demand.

The numbers come from the planner's three real routing strategies, run by
:class:`dispatch_support.PlannerStrategyRunner`, and summarised by
:class:`droneops_dispatcher.dispatch.ScenarioComparison` - which owns the fairness
of the experiment rather than the routing itself. The configuration (strategy names,
seed list, what is held constant) is read from
``simulation/scenarios/dispatch-competition-v1/comparison.yaml`` so the comparison
is a versioned scenario, not a test fixture with opinions in it.
"""

from __future__ import annotations

from collections.abc import Sequence

import dispatch_support as sup
import pytest

from droneops_dispatcher import (
    GUARDED_INPUT_FIELDS,
    MIN_COMPARISON_REPETITIONS,
    RANK_METRICS,
    RepositioningLeg,
    RunOutcome,
    ScenarioComparison,
    WeakenedBaselineError,
    best_repositioning,
    price_repositioning,
)
from droneops_facilities import WaitReason, alternate_site_quotes

pytestmark = pytest.mark.slow
STRATEGIES = ("constraint_aware_direct", "hybrid", "structured_corridor")


def _runner() -> sup.PlannerStrategyRunner:
    return sup.PlannerStrategyRunner(sup.bundle_profiles(), order_count=8)


def _seeds() -> tuple[int, ...]:
    config = sup.comparison_config()
    return tuple(int(seed) for seed in config["seeds"])


def _configured_strategies() -> tuple[str, ...]:
    config = sup.comparison_config()
    return tuple(str(name) for name in config["strategies"])


@pytest.fixture(scope="module")
def report() -> object:
    comparison = ScenarioComparison(
        runner=_runner(),
        strategies=_configured_strategies(),
        seeds=_seeds(),
        policy=sup.policy_with(),
    )
    return comparison.run()


def test_three_routing_strategies_over_the_seeded_repetitions(report: object) -> None:
    """Median and spread over >= 10 seeds, per strategy, on identical demand."""
    seeds = _seeds()
    assert len(seeds) >= MIN_COMPARISON_REPETITIONS
    assert _configured_strategies() == STRATEGIES
    for name in STRATEGIES:
        summary = report.of(name)  # type: ignore[attr-defined]
        assert summary.repetitions == len(seeds)
        assert summary.median("completed") > 0, f"{name} completed nothing in any seed"
        assert summary.spread("completed") >= 0
        for metric in RANK_METRICS:
            row = summary.summary_of(metric)
            assert row.samples == len(seeds)
            assert row.minimum <= row.median <= row.maximum
            assert row.spread == row.maximum - row.minimum
    # The arms really are different strategies: identical inputs, different answers.
    medians = {name: report.of(name).median("energy_wh") for name in STRATEGIES}  # type: ignore[attr-defined]
    assert len(set(medians.values())) >= 2, (
        f"strategies produced no measurable difference: {medians}"
    )


def test_every_seed_gave_every_strategy_the_very_same_world(report: object) -> None:
    seeds = _seeds()
    for seed in seeds:
        per_seed: dict[str, Sequence[object]] = {}
        for name in STRATEGIES:
            rows = [row for row in report.of(name).runs if row.seed == seed]  # type: ignore[attr-defined]
            assert len(rows) == 1
            per_seed[name] = [getattr(rows[0], field) for field in GUARDED_INPUT_FIELDS]
        assert len({tuple(row) for row in per_seed.values()}) == 1, (
            f"seed {seed}: arms were given different inputs"
        )
    checks = dict(report.checks)  # type: ignore[attr-defined]
    assert "identical_demand" in checks
    assert "one_solver_worker" in checks
    assert str(len(seeds)) in checks["repetitions"]


def test_comparison_reports_median_and_spread_as_a_table(report: object) -> None:
    rows = report.table()  # type: ignore[attr-defined]
    assert rows[0][0] == "strategy"
    assert len(rows) == len(STRATEGIES) + 1
    printed = report.render()  # type: ignore[attr-defined]
    print(
        "\ndispatch-competition-v1: median/spread over", len(_seeds()), "seeds\n", printed, sep=""
    )
    assert "median/spread" in printed
    for name in STRATEGIES:
        assert name in printed
    document = report.as_document()  # type: ignore[attr-defined]
    assert document["banner"].startswith("Simulation only")
    assert len(document["seeds"]) == len(_seeds())  # type: ignore[arg-type]


def test_twenty_one_seeded_runs_cover_completion_lateness_energy_and_repositioning(
    report: object,
) -> None:
    """Phase 3's exit criterion: at least 20 seeded scenarios, four trade-offs shown."""
    seeds = _seeds()
    assert len(seeds) >= 20
    profiles = sup.bundle_profiles()
    origin, target = profiles[0], profiles[1]
    policy = sup.policy_with()
    scheduler_repositioning_wh: list[int] = []
    for seed in seeds:
        quotes = alternate_site_quotes(
            f"m-{seed}",
            origin,
            (target,),
            WaitReason.PAD_WAIT,
            lambda identity: (),
            at_us=origin.opens_us,
            origin_wait_us=(seed % 11) * sup.MINUTE_US,
        )
        priced = price_repositioning(
            quotes,
            (RepositioningLeg(target_facility_id=target.facility_id, leg_mm=6_000_000),),
            policy=policy,
            cruise_power_w=200,
            reference_airspeed_dmps=150,
        )
        chosen = best_repositioning(priced)
        assert chosen is not None
        scheduler_repositioning_wh.append(chosen.energy_wh)
    assert sum(scheduler_repositioning_wh) > 0
    for metric in ("completed", "mean_lateness_us", "energy_wh"):
        assert all(metric in report.of(name).summary_of(metric).metric for name in STRATEGIES)  # type: ignore[attr-defined]


# ------------------------------------------------------------------ fairness guard
def _outcome(strategy: str, seed: int, *, fleet_size: int = 10) -> RunOutcome:
    """A synthetic run row: enough to exercise the comparison's fairness guard."""
    return RunOutcome(
        strategy=strategy,
        seed=seed,
        completed=fleet_size,
        late=0,
        unserved=0,
        rejected=0,
        energy_wh=100 + seed % 7,
        mean_lateness_us=0,
        pad_wait_us=0,
        crew_wait_us=0,
        charger_wait_us=0,
        crossings=0,
        recompute_attempts=0,
        cost_units=1,
        demand_digest=f"demand-{seed}",
        inputs_digest=f"inputs-{seed}",
        fleet_size=fleet_size,
        pad_capacity=4,
        charger_capacity=4,
        crew_capacity=2,
        cutoff_us=90 * sup.MINUTE_US,
        solver_workers=1,
        energy_profile_digest="profile-x",
    )


class _StubRunner:
    """A fake arm: identical unless deliberately weakened."""

    def __init__(self, weakens: str | None = None) -> None:
        self.weakens = weakens

    def run(self, strategy: str, *, seed: int) -> RunOutcome:
        fleet = 9 if strategy == self.weakens else 10
        return _outcome(strategy, seed, fleet_size=fleet)


def test_a_weakened_baseline_is_refused_not_reported() -> None:
    comparison = ScenarioComparison(
        runner=_StubRunner(weakens="structured_corridor"),
        strategies=STRATEGIES,
        seeds=tuple(range(1, MIN_COMPARISON_REPETITIONS + 1)),
        policy=sup.policy_with(),
    )
    with pytest.raises(WeakenedBaselineError, match="fleet_size"):
        comparison.run()


def test_a_mismatched_arm_identity_is_refused() -> None:
    class _Wrong:
        def run(self, strategy: str, *, seed: int) -> RunOutcome:
            return _outcome("something_else" if strategy == "hybrid" else strategy, seed)

    comparison = ScenarioComparison(
        runner=_Wrong(),
        strategies=STRATEGIES,
        seeds=tuple(range(1, MIN_COMPARISON_REPETITIONS + 1)),
        policy=sup.policy_with(),
    )
    with pytest.raises(WeakenedBaselineError, match="not comparable"):
        comparison.run()


def test_too_few_seeded_repetitions_are_refused() -> None:
    with pytest.raises(Exception, match=f"{MIN_COMPARISON_REPETITIONS}"):
        ScenarioComparison(
            runner=_StubRunner(),
            strategies=STRATEGIES,
            seeds=(1, 2, 3),
            policy=sup.policy_with(),
        )
    with pytest.raises(Exception, match="duplicate seeds"):
        ScenarioComparison(
            runner=_StubRunner(),
            strategies=STRATEGIES,
            seeds=tuple([1] * MIN_COMPARISON_REPETITIONS),
            policy=sup.policy_with(),
        )


def test_the_scenario_document_declares_every_guarded_input() -> None:
    config = sup.comparison_config()
    held = dict(config["held_constant"])
    assert {"demand", "weather", "aircraft_states", "seed", "cutoff", "energy_profiles"} <= set(
        held
    )
    assert {"pad_capacity", "crew_capacity", "solver_workers"} <= set(held)
    assert str(config["scenario_id"]) == "dispatch-competition-v1"
    assert "what_this_does_not_prove" in config
    assert GUARDED_INPUT_FIELDS
    assert "demand_digest" in GUARDED_INPUT_FIELDS
