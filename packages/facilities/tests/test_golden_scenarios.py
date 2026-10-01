"""The section 6 golden scenarios: pad contention and charger contention.

Each scenario is the five-field record ``technical-build/06`` section 6 requires,
replayed by :func:`run_golden` against a metric vector. The scenarios exist to
be read as much as executed, which is why the "what this does not prove" entries
name the specific adjacent claims these runs cannot support - a saturated hub is
not a validated hub, and a bay that queued correctly is not a bay that was
energy-feasible.

Also checked here: the scenario type cannot be constructed with the
does-not-prove field emptied, mirroring the conflict checker's bundle rule.
"""

from __future__ import annotations

import pytest
from facilities_support import SECOND_US, load_bundle_document, make_profile, make_request

from droneops_facilities import (
    I6_INVARIANT,
    FacilityQueue,
    GoldenScenario,
    ScenarioError,
    ScenarioInput,
    WaitReason,
    load_capacity_profiles,
    run_golden,
)

pytestmark = pytest.mark.golden

# 80 s chain, 50 s crewed span starting 20 s in: see facilities_support.
SATURATED_PADS = "hub-golden-pad"
CONTENDED_CHARGERS = "hub-golden-charger"

PAD_CONTENTION = GoldenScenario(
    name="pad-contention",
    description=(
        "One pad, three simultaneous arrivals at a staffed hub. Landing resource "
        "saturation must show up as measured wait for the later arrivals, never as "
        "two aircraft on one pad."
    ),
    input=ScenarioInput(
        profiles=(make_profile(SATURATED_PADS, pads=1, charger_bays=2, crew_slots=2),),
        requests=tuple(
            make_request(f"pad-{index}", SATURATED_PADS, ready_s=0, recharge_us=60 * SECOND_US)
            for index in range(3)
        ),
    ),
    expected_invariant=I6_INVARIANT,
    expected_metrics={
        "requests": 3,
        "scheduled": 3,
        "queued": 0,
        "i6_holds": 1,
        f"peak_pad_overlap::{SATURATED_PADS}": 1,
        f"pad_capacity::{SATURATED_PADS}": 1,
        "pad_wait_us": 240 * SECOND_US,
        "charger_wait_us": 0,
        "crew_wait_us": 0,
    },
    does_not_prove=(
        "that one pad is the right number for a real site: the count is synthetic",
        "that a queued aircraft can still make its delivery window: lateness is a "
        "dispatch and energy question, not a facility one",
        "that the pad geometry, approach profile or obstacle clearance is safe: "
        "Chandigarh OSM carries no verified vertical data",
        "anything about concurrent PostgreSQL commits; this is one scheduler, one order",
    ),
)

PAD_CONTENTION_CUTOFF = GoldenScenario(
    name="pad-contention-cutoff",
    description=(
        "One pad, three arrivals, and a working day that ends before the queue "
        "drains. The overflow must sit in a queue with a reason; it must not vanish "
        "and must not be squeezed onto the pad."
    ),
    input=ScenarioInput(
        profiles=(make_profile(SATURATED_PADS, pads=1, charger_bays=2, crew_slots=2, closes_min=2),),
        requests=tuple(
            make_request(f"cut-{index}", SATURATED_PADS, ready_s=0, deadline_s=100) for index in range(3)
        ),
    ),
    expected_invariant=I6_INVARIANT,
    expected_metrics={
        "requests": 3,
        "scheduled": 1,
        "queued": 2,
        "rejected": 0,
        "i6_holds": 1,
        "queue_growth": 2,
        "peak_queue": 2,
        f"peak_pad_overlap::{SATURATED_PADS}": 1,
    },
    does_not_prove=(
        "that the waiting work is still worth doing: re-offering it to another site "
        "is the dispatcher's decision",
        "that a starvation cap fired: no cap is configured in this scenario",
        "that the queue drains fairly when priorities differ: preferences are equal here",
    ),
)

CHARGER_CONTENTION = GoldenScenario(
    name="charger-contention",
    description=(
        "Two pads and two crew slots but a single charger bay, with a recharge that "
        "outlasts the pad chain. The battery turnaround is the bottleneck: both "
        "aircraft land, the second one waits at the bay."
    ),
    input=ScenarioInput(
        profiles=(make_profile(CONTENDED_CHARGERS, pads=2, charger_bays=1, crew_slots=2),),
        requests=tuple(
            make_request(f"chg-{index}", CONTENDED_CHARGERS, ready_s=0, recharge_us=300 * SECOND_US)
            for index in range(2)
        ),
    ),
    expected_invariant=I6_INVARIANT,
    expected_metrics={
        "requests": 2,
        "scheduled": 2,
        "queued": 0,
        "i6_holds": 1,
        f"peak_pad_overlap::{CONTENDED_CHARGERS}": 2,
        f"pad_capacity::{CONTENDED_CHARGERS}": 2,
        f"peak_charger_overlap::{CONTENDED_CHARGERS}": 1,
        f"charger_capacity::{CONTENDED_CHARGERS}": 1,
        "pad_wait_us": 0,
        "crew_wait_us": 0,
        "charger_wait_us": 300 * SECOND_US,
    },
    does_not_prove=(
        "that 300 s is a real recharge time or that the aircraft needed it: the "
        "battery curve belongs to the energy package",
        "that the queue is energy-safe: an aircraft waiting at a bay is a "
        "reserve-margin question the facility scheduler cannot answer",
        "that one bay is enough throughput: this scenario measures a bottleneck, "
        "it does not size one",
    ),
)

SCENARIOS = (PAD_CONTENTION, PAD_CONTENTION_CUTOFF, CHARGER_CONTENTION)


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda scenario: scenario.name)
def test_golden_scenario_matches_its_expected_metrics(scenario: GoldenScenario) -> None:
    run = run_golden(scenario)
    assert run.passed, run.summary()
    assert run.metrics["i6_holds"] == 1


def test_pad_contention_waits_are_pad_shaped_not_charger_shaped() -> None:
    run = run_golden(PAD_CONTENTION)
    assert run.metrics["pad_wait_us"] > 0
    assert run.metrics["charger_wait_us"] == 0
    assert run.metrics[f"peak_pad_overlap::{SATURATED_PADS}"] == run.metrics[f"pad_capacity::{SATURATED_PADS}"]


def test_charger_contention_is_attributed_to_the_bay_not_the_pad() -> None:
    run = run_golden(CHARGER_CONTENTION)
    assert run.metrics["charger_wait_us"] == 300 * SECOND_US
    audit = next(audit for audit in run.audits if audit.facility_id == CONTENDED_CHARGERS)
    assert audit.pads.peak_overlap == audit.pads.capacity
    assert audit.chargers.peak_overlap == audit.chargers.capacity
    assert audit.chargers.double_booked_identities == ()


def test_overflow_carries_a_named_cause_and_not_a_silent_none() -> None:
    from droneops_facilities import FacilityQueue

    queue = FacilityQueue()
    run = run_golden(PAD_CONTENTION_CUTOFF, queue=queue)
    assert run.passed, run.summary()
    assert [entry.reason for entry in queue.pending()] == [WaitReason.PAD_WAIT, WaitReason.PAD_WAIT]
    assert all(entry.reason is not WaitReason.NONE for entry in queue.pending())
    assert all(entry.request.allow_queue for entry in queue.pending())


def test_the_cutoff_scenario_reports_a_queue_rather_than_a_loss() -> None:
    run = run_golden(PAD_CONTENTION_CUTOFF)
    assert run.passed, run.summary()
    assert run.metrics["scheduled"] + run.metrics["queued"] + run.metrics["rejected"] == 3


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda scenario: scenario.name)
def test_every_scenario_carries_the_five_section_6_fields(scenario: GoldenScenario) -> None:
    assert scenario.description and scenario.input.profiles and scenario.input.requests
    assert scenario.expected_invariant == I6_INVARIANT
    assert scenario.expected_metrics
    assert scenario.does_not_prove
    assert all(entry.strip() for entry in scenario.does_not_prove)
    assert len({entry for entry in scenario.does_not_prove}) == len(scenario.does_not_prove)


def test_a_silent_scenario_cannot_be_constructed() -> None:
    with pytest.raises(ScenarioError, match="does not prove"):
        GoldenScenario(
            name="too-quiet",
            description="claims everything, proves nothing",
            input=PAD_CONTENTION.input,
            expected_invariant=I6_INVARIANT,
            expected_metrics={"scheduled": 3},
            does_not_prove=(),
        )


def test_a_metric_drift_is_detected_not_tolerated() -> None:
    drifted = GoldenScenario(
        name="pad-contention-drift",
        description=PAD_CONTENTION.description,
        input=PAD_CONTENTION.input,
        expected_invariant=PAD_CONTENTION.expected_invariant,
        expected_metrics={**PAD_CONTENTION.expected_metrics, "pad_wait_us": 1},
        does_not_prove=PAD_CONTENTION.does_not_prove,
    )
    run = run_golden(drifted)
    assert not run.passed
    assert ("pad_wait_us", 1, 240 * SECOND_US) in run.differing
    assert "pad_wait_us" in run.summary()


def test_an_unknown_site_in_the_input_is_a_scenario_error() -> None:
    broken = GoldenScenario(
        name="ghost-site",
        description="a scenario that names a site it never declared",
        input=ScenarioInput(
            profiles=PAD_CONTENTION.input.profiles,
            requests=(make_request("stray", "hub-that-does-not-exist"),),
        ),
        expected_invariant=I6_INVARIANT,
        expected_metrics={"scheduled": 1},
        does_not_prove=("nothing about sites that do not exist",),
    )
    with pytest.raises(ScenarioError, match="unknown site"):
        run_golden(broken)


def test_the_shipped_bundle_can_be_replayed_as_a_golden_scenario() -> None:
    profiles = load_capacity_profiles(load_bundle_document())
    rimroad = next(profile for profile in profiles if profile.facility_id == "node-rimroad-synthetic")
    assert rimroad.pad_count == 1 and rimroad.charger_bays == 1
    scenario = GoldenScenario(
        name="pad-contention-shipped-bundle",
        description=(
            "The single-pad roadside node from the synthetic bundle, four arrivals "
            "in the first hour."
        ),
        input=ScenarioInput(
            profiles=(rimroad,),
            requests=tuple(
                make_request(f"bundle-{index}", rimroad.facility_id, ready_s=index * 30, recharge_us=90 * SECOND_US)
                for index in range(4)
            ),
        ),
        expected_invariant=I6_INVARIANT,
        expected_metrics={
            "requests": 4,
            f"peak_pad_overlap::{rimroad.facility_id}": 1,
            f"pad_capacity::{rimroad.facility_id}": 1,
            f"peak_charger_overlap::{rimroad.facility_id}": 1,
            "i6_holds": 1,
        },
        does_not_prove=(
            "that the shipped site counts reflect surveyed capacity",
            "that four arrivals is realistic demand for a roadside node",
        ),
    )
    run = run_golden(scenario)
    assert run.passed, run.summary()


def test_a_queued_reason_is_never_reported_as_no_reason() -> None:
    queue = FacilityQueue()
    run = run_golden(PAD_CONTENTION_CUTOFF, queue=queue)
    assert run.passed, run.summary()
    assert len(queue) == 2
    assert [entry.reason for entry in queue.pending()] == [WaitReason.PAD_WAIT, WaitReason.PAD_WAIT]
