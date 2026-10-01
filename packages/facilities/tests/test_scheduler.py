"""Behaviour of the facility scheduler: queueing, chain placement, authority.

Every test here drives one of the realistic failures named in the spec - a pad
free with no crew, a crew free with the charger occupied, a chain that has to
step over a shift boundary - and asserts the *reason code* as well as the
outcome, because a wait that cannot be named cannot be shown in a report.

What this does not prove: that the placement policy is optimal. First-fit on the
earliest feasible start can leave capacity idle that a global search would use,
and choosing between those trade-offs belongs to the dispatcher.
"""

from __future__ import annotations

import pytest
from facilities_support import (
    CHAIN_US,
    CREW_US,
    PRE_CREW_US,
    SECOND_US,
    make_claim,
    make_profile,
    make_request,
)

from droneops_facilities import (
    AppendOnlyLedger,
    ClaimPurpose,
    CommitConflictError,
    DecisionState,
    FacilityScheduler,
    OutageWindow,
    TurnaroundDurations,
    WaitReason,
    alternate_site_quotes,
    order_arrivals,
    overlap_count,
    profile_in_outage,
)
from droneops_geom_core import Interval, ResourceKind

pytestmark = pytest.mark.unit


# --- the chain --------------------------------------------------------------


def test_a_quiet_site_places_the_whole_turnaround_chain() -> None:
    scheduler = FacilityScheduler([make_profile("hub-chain", pads=2, charger_bays=2)])
    decision = scheduler.propose(
        make_request("req-chain", "hub-chain", ready_s=0, recharge_us=60 * SECOND_US)
    )
    plan = decision.plan
    assert plan is not None and decision.state is DecisionState.SCHEDULED

    by_stage = {stage.stage.value: stage.window for stage in plan.stages}
    assert set(by_stage) == {"approach", "landing", "handover", "loading", "departure", "recharge"}
    assert by_stage["approach"] == Interval.of(0, 10 * SECOND_US)
    assert by_stage["landing"].start == by_stage["approach"].end
    assert by_stage["handover"].start == by_stage["landing"].end
    assert by_stage["loading"].end == by_stage["departure"].start
    assert by_stage["departure"].end == plan.end_us == CHAIN_US
    # Recharge is a separate resource held only after the pad is released.
    assert by_stage["recharge"].start == CHAIN_US
    assert by_stage["recharge"].measure == 60 * SECOND_US


def test_chain_claims_are_three_distinct_canonical_resources() -> None:
    plan = FacilityScheduler([make_profile("hub-keys")]).propose(
        make_request("req-keys", "hub-keys", recharge_us=60 * SECOND_US)
    ).plan
    assert plan is not None
    keys = {claim.purpose: claim.key.canonical for claim in plan.claims}
    assert keys[ClaimPurpose.PAD].startswith("pad:pad-hub-keys-0:")
    assert keys[ClaimPurpose.CREW].startswith("crew:crew-hub-keys-0:")
    assert keys[ClaimPurpose.CHARGER].startswith("charger:charger-hub-keys-0:")
    crew_claim = next(claim for claim in plan.claims if claim.purpose is ClaimPurpose.CREW)
    pad_claim = next(claim for claim in plan.claims if claim.purpose is ClaimPurpose.PAD)
    # The crewed span is a fixed offset inside the pad claim, never after it.
    assert pad_claim.window.contains(crew_claim.window)
    assert crew_claim.window.start - pad_claim.window.start == PRE_CREW_US
    assert crew_claim.window.measure == CREW_US


def test_a_mission_with_no_recharge_need_holds_no_charger() -> None:
    plan = FacilityScheduler([make_profile("hub-drop")]).propose(
        make_request("req-drop", "hub-drop", requires_recharge=False)
    ).plan
    assert plan is not None
    assert {claim.purpose for claim in plan.claims} == {ClaimPurpose.PAD, ClaimPurpose.CREW}
    assert plan.claims_for(ClaimPurpose.CHARGER) == ()


# --- queueing, never overbooking -------------------------------------------


def test_pad_saturation_grows_a_queue_instead_of_overbooking() -> None:
    profile = make_profile("hub-saturated", pads=2, charger_bays=4, crew_slots=4)
    scheduler = FacilityScheduler([profile])
    arrivals = tuple(
        make_request(f"req-{index}", "hub-saturated", ready_s=0, deadline_s=100) for index in range(5)
    )
    outcome = scheduler.schedule(arrivals)

    assert len(outcome.scheduled) == 2
    assert len(outcome.queued) == 3
    assert outcome.rejected == ()
    audit = scheduler.audit("hub-saturated")
    assert audit.pads.peak_overlap == 2 == audit.pads.capacity == profile.pad_count
    assert outcome.i6_holds is True
    assert outcome.metrics["queued"] == 3
    assert outcome.metrics["peak_queue"] == 3


def test_a_held_request_that_cannot_be_served_still_lands_on_the_pad() -> None:
    """Saturation inside the day is measured wait, not rejection."""
    profile = make_profile("hub-late-shift", pads=1, charger_bays=2, crew_slots=2)
    scheduler = FacilityScheduler([profile])
    scheduler.propose(make_request("req-first", "hub-late-shift"))
    second = scheduler.propose(make_request("req-second", "hub-late-shift"))
    assert second.plan is not None and second.plan.start_us == CHAIN_US
    assert second.plan.wait_us(WaitReason.PAD_WAIT) == CHAIN_US
    assert second.plan.wait_us(WaitReason.CHARGER_WAIT) == 0
    assert scheduler.audit("hub-late-shift").pads.peak_overlap == 1


def test_a_queued_request_is_rejected_with_a_reason_when_queueing_is_off() -> None:
    profile = make_profile("hub-strict", pads=1, charger_bays=2, crew_slots=2)
    scheduler = FacilityScheduler([profile])
    first = scheduler.propose(make_request("req-first", "hub-strict", deadline_s=100))
    second = scheduler.propose(make_request("req-second", "hub-strict", deadline_s=100, allow_queue=False))
    assert first.granted
    assert second.state is DecisionState.REJECTED
    assert second.reason is WaitReason.PAD_WAIT
    assert second.rejection is not None
    # Nothing frees inside the horizon, so the honest answer is "no estimate".
    assert second.rejection.earliest_retry_us is None
    assert len(scheduler.queue) == 0
    assert scheduler.audit("hub-strict").pads.peak_overlap == 1


def test_queue_length_is_monotonic_under_more_arrivals_than_pads() -> None:
    scheduler = FacilityScheduler([make_profile("hub-grow", pads=1, charger_bays=3, crew_slots=3)])
    lengths = []
    for index in range(6):
        scheduler.propose(
            make_request(f"req-{index}", "hub-grow", ready_s=index * 10, deadline_s=100)
        )
        lengths.append(len(scheduler.queue))
    assert lengths == [0, 1, 2, 3, 4, 5]
    assert scheduler.audit("hub-grow").pads.peak_overlap <= 1


# --- the two named bottlenecks --------------------------------------------


def test_pad_free_but_no_crew_is_a_cause_of_its_own() -> None:
    """A hub with three empty pads and one crew team cannot take three jobs."""
    profile = make_profile("hub-nocrew", pads=3, charger_bays=4, crew_slots=1, crew_shifts=((0, 5),))
    scheduler = FacilityScheduler([profile])
    first = scheduler.propose(make_request("req-early", "hub-nocrew"))
    # The pads are wide open at minute 4; the crew went home at minute 5.
    later = scheduler.propose(make_request("req-late", "hub-nocrew", ready_s=4 * 60))

    assert first.granted and first.plan is not None
    assert later.plan is None and later.state is DecisionState.QUEUED
    assert later.reason is WaitReason.CREW_UNAVAILABLE
    assert later.rejection is not None and "no crew window" in later.rejection.detail
    assert scheduler.audit("hub-nocrew").pads.peak_overlap == 1
    assert scheduler.audit("hub-nocrew").crew.peak_overlap == 1 <= scheduler.audit("hub-nocrew").crew.capacity


def test_a_late_crew_gap_costs_a_measured_crew_wait_not_a_pad_wait() -> None:
    profile = make_profile("hub-crewgap", pads=2, charger_bays=2, crew_slots=1)
    scheduler = FacilityScheduler([profile])
    first = scheduler.propose(make_request("req-1", "hub-crewgap"))
    second = scheduler.propose(make_request("req-2", "hub-crewgap"))
    assert first.granted and second.granted
    assert second.plan is not None
    assert second.plan.wait_us(WaitReason.PAD_WAIT) == 0
    assert second.plan.wait_us(WaitReason.CREW_UNAVAILABLE) == CREW_US


def test_crew_free_but_charger_occupied_is_a_charger_wait() -> None:
    profile = make_profile("hub-charger", pads=4, charger_bays=1, crew_slots=4)
    scheduler = FacilityScheduler([profile])
    first = scheduler.propose(make_request("req-a", "hub-charger", recharge_us=300 * SECOND_US))
    second = scheduler.propose(make_request("req-b", "hub-charger", recharge_us=300 * SECOND_US))

    assert first.granted and second.granted
    assert first.plan is not None and second.plan is not None
    assert first.plan.wait_us(WaitReason.CHARGER_WAIT) == 0
    # The second aircraft gets a pad and crew immediately but queues at the one bay.
    assert second.plan.wait_us(WaitReason.CHARGER_WAIT) == 300 * SECOND_US
    assert second.plan.wait_us(WaitReason.PAD_WAIT) == 0
    audit = scheduler.audit("hub-charger")
    assert audit.chargers.peak_overlap == 1 == audit.chargers.capacity
    assert audit.pads.peak_overlap == 2 <= audit.pads.capacity
    assert second.plan.end_us - second.plan.start_us == CHAIN_US


def test_reason_codes_do_not_collapse_into_one_bucket() -> None:
    profile = make_profile("hub-causes", pads=1, charger_bays=1, crew_slots=1)
    scheduler = FacilityScheduler([profile])
    scheduler.propose(make_request("req-1", "hub-causes", recharge_us=300 * SECOND_US))
    scheduler.propose(make_request("req-2", "hub-causes", recharge_us=300 * SECOND_US))
    metrics = scheduler.metrics()
    assert metrics["pad_wait_us"] > 0 and metrics["charger_wait_us"] > 0
    assert metrics["wait_us_pad_wait"] == metrics["pad_wait_us"]
    assert metrics["wait_us_charger_wait"] == metrics["charger_wait_us"]


# --- opening hours, shift boundaries, outages ------------------------------


def test_a_chain_completes_across_a_crew_shift_boundary() -> None:
    """The lakeview pattern: staffed 0-25 and 30-60, with a gap in the middle."""
    profile = make_profile("hub-shift", pads=2, charger_bays=2, crew_shifts=((0, 25), (30, 60)))
    scheduler = FacilityScheduler([profile])
    decision = scheduler.propose(
        make_request("req-shift", "hub-shift", ready_s=24 * 60, recharge_us=60 * SECOND_US)
    )
    plan = decision.plan
    assert plan is not None, decision.rejection

    # The crewed 50 s span cannot finish before the 25-minute close, so the whole
    # chain steps to 20 s before the next shift opens and then completes.
    assert plan.start_us == 29 * 60 * SECOND_US + 40 * SECOND_US
    crew_claim = next(claim for claim in plan.claims if claim.purpose is ClaimPurpose.CREW)
    assert Interval.of(30 * 60 * SECOND_US, 60 * 60 * SECOND_US).contains(crew_claim.window)
    assert plan.wait_us(WaitReason.CREW_UNAVAILABLE) == 340 * SECOND_US
    assert plan.wait_us(WaitReason.PAD_WAIT) == 0
    assert plan.end_us < 60 * 60 * SECOND_US
    assert plan.stages[-1].stage.value == "recharge"
    assert scheduler.audit("hub-shift").i6_holds


def test_nothing_is_scheduled_outside_opening_hours() -> None:
    profile = make_profile("hub-late", closes_min=1)
    scheduler = FacilityScheduler([profile])
    decision = scheduler.propose(make_request("req-closed", "hub-late", ready_s=90, deadline_s=200))
    assert decision.plan is None
    assert decision.reason is WaitReason.OUTSIDE_OPENING_HOURS


def test_an_outage_mid_shift_refuses_new_work_and_keeps_committed_work() -> None:
    committed = make_claim(
        "req-committed", facility_id="hub-outage", index=0, start_s=38 * 60, end_s=43 * 60
    )
    ledger = AppendOnlyLedger((committed,))
    profile = profile_in_outage(
        make_profile("hub-outage", pads=2, charger_bays=2, crew_slots=2),
        OutageWindow(41 * 60 * SECOND_US, 49 * 60 * SECOND_US),
    )
    scheduler = FacilityScheduler([profile], committed=ledger)

    held_before = ledger.claims_on("pad:pad-hub-outage-0")
    refused = scheduler.propose(
        make_request("req-inside", "hub-outage", ready_s=42 * 60, deadline_s=45 * 60, allow_queue=False)
    )
    assert refused.plan is None and refused.reason is WaitReason.FACILITY_OUTAGE
    assert refused.rejection is not None and "committed claims stay on the record" in refused.rejection.detail
    # The asymmetry: nothing new is accepted, and nothing already held is dropped.
    assert ledger.claims_on("pad:pad-hub-outage-0") == held_before
    assert len(held_before) == 1 and held_before[0].window == committed.window
    assert scheduler.audit("hub-outage").pads.peak_overlap == 1 <= scheduler.audit("hub-outage").pads.capacity

    impact = scheduler.outage_impact("hub-outage", 42 * 60 * SECOND_US)
    assert impact.blocks_new_claims and impact.committed_claims_preserved
    assert impact.requires_reconciliation and len(impact.affected_claims) == 1

    after = scheduler.propose(make_request("req-after", "hub-outage", ready_s=42 * 60))
    assert after.plan is not None and after.plan.start_us >= 49 * 60 * SECOND_US


def test_a_superseding_correction_never_edits_the_original_record() -> None:
    ledger = AppendOnlyLedger()
    scheduler = FacilityScheduler([make_profile("hub-super", pads=2, charger_bays=2)], committed=ledger)
    original = scheduler.propose(make_request("req-orig", "hub-super")).plan
    assert original is not None
    ledger.commit(original, recorded_at_us=0)
    # The ledger confirms and the scheduler releases its own hold: two
    # authorities, one handover, and no window where the pad is free twice.
    assert scheduler.settle(original.proposal_id) is original
    replacement = scheduler.propose(make_request("req-orig", "hub-super", ready_s=200)).plan
    assert replacement is not None
    with pytest.raises(CommitConflictError):
        ledger.supersede(original, original, recorded_at_us=0)
    assert len(ledger.claims_on("pad:pad-hub-super-0")) == 1
    assert len(scheduler.outstanding()) == 1


def test_an_astray_claim_cannot_be_forced_past_the_ledger_boundary() -> None:
    """Even a hand-built double booking is refused where it would be committed."""
    profile = make_profile("hub-force", pads=1, charger_bays=1)
    scheduler = FacilityScheduler([profile])
    plan = scheduler.propose(make_request("req-in-order", "hub-force")).plan
    assert plan is not None
    pad_claim = plan.claims_for(ClaimPurpose.PAD)[0]
    stray = make_claim("req-stray", facility_id="hub-force", index=0, start_s=40, end_s=120)
    assert stray.identity_token == pad_claim.identity_token
    # The scheduler never saw the stray request, so its own audit still passes;
    # the ledger is the layer that refuses it.
    with pytest.raises(CommitConflictError):
        AppendOnlyLedger((pad_claim,)).append(stray, recorded_at_us=0)
    assert overlap_count(tuple(claim.window for claim in (pad_claim, stray))) == 2 > profile.pad_count
    assert scheduler.audit("hub-force").pads.peak_overlap == 1


# --- determinism and the ledger boundary -----------------------------------


def test_equal_preference_arrivals_are_ordered_by_the_documented_tiebreak() -> None:
    profile = make_profile("hub-tie", pads=1, charger_bays=3, crew_slots=3)
    simultaneous = (
        make_request("req-zulu", "hub-tie", deadline_s=100),
        make_request("req-alpha", "hub-tie", deadline_s=100),
    )
    assert [request.request_id for request in order_arrivals(simultaneous)] == [
        "req-alpha",
        "req-zulu",
    ]

    results = []
    for batch in (simultaneous, tuple(reversed(simultaneous))):
        scheduler = FacilityScheduler([profile])
        outcome = scheduler.schedule(batch)
        assert len(outcome.scheduled) == 1
        assert outcome.scheduled[0].request_id == "req-alpha"
        assert outcome.scheduled[0].claims_for(ClaimPurpose.PAD)[0].identity_token == (
            "pad:pad-hub-tie-0"
        )
        assert len(outcome.queued) == 1
        results.append(outcome.scheduled[0].proposal_id)
    assert results[0] == results[1]


def test_preference_beats_arrival_order_in_the_documented_tiebreak() -> None:
    profile = make_profile("hub-pref", pads=1, charger_bays=3, crew_slots=3)
    urgent_late = make_request("req-urgent", "hub-pref", ready_s=10, preference=0)
    calm_early = make_request("req-calm", "hub-pref", ready_s=0, preference=1)
    outcome = FacilityScheduler([profile]).schedule((calm_early, urgent_late))
    assert outcome.scheduled[0].request_id == "req-urgent"
    assert outcome.scheduled[0].start_us == 10 * SECOND_US


def test_the_same_request_twice_makes_the_same_proposal_not_a_second_claim() -> None:
    scheduler = FacilityScheduler([make_profile("hub-idem", pads=2, charger_bays=2)])
    request = make_request("req-idem", "hub-idem")
    first = scheduler.propose(request).plan
    assert first is not None
    assert scheduler.settle(first.proposal_id) is first
    again = scheduler.propose(request).plan
    assert again is not None and again.proposal_id == first.proposal_id
    # The ledger has not taken either copy, so the pad is still blocked once.
    assert scheduler.audit("hub-idem").pads.peak_overlap == 1


def test_a_scheduler_never_mutates_a_committed_claim() -> None:
    committed = make_claim("req-held", facility_id="hub-mutate", index=0, start_s=0, end_s=100)
    ledger = AppendOnlyLedger((committed,))
    scheduler = FacilityScheduler([make_profile("hub-mutate", pads=2, charger_bays=2)], committed=ledger)
    before = ledger.claims_on("pad:pad-hub-mutate-0")
    scheduler.schedule((make_request("req-new", "hub-mutate"),))
    assert ledger.claims_on("pad:pad-hub-mutate-0") == before
    with pytest.raises((AttributeError, TypeError)):
        before[0].window = Interval.of(0, 1)  # type: ignore[misc]


def test_the_ledger_boundary_rejects_a_double_booking_even_if_asked() -> None:
    ledger = AppendOnlyLedger()
    scheduler = FacilityScheduler([make_profile("hub-ledger", pads=1, charger_bays=2)], committed=ledger)
    plan = scheduler.propose(make_request("req-1", "hub-ledger", recharge_us=60 * SECOND_US)).plan
    assert plan is not None
    receipt = ledger.commit(plan, recorded_at_us=0)
    assert receipt.claim_count == len(plan.claims)
    # The receipt lists its keys in canonical lock order: pads, then chargers,
    # then crew - the order the transaction must acquire them in.
    assert tuple(key.split(":", 1)[0] for key in receipt.keys) == ("pad", "charger", "crew")
    assert ledger.commit(plan, recorded_at_us=0).proposal_id == receipt.proposal_id
    with pytest.raises(CommitConflictError):
        ledger.append(
            make_claim("req-intruder", facility_id="hub-ledger", index=0, start_s=40, end_s=60),
            recorded_at_us=0,
        )
    assert scheduler.audit("hub-ledger").i6_holds


def test_an_offered_but_unsettled_pad_stays_blocked() -> None:
    """A proposal the ledger never took must not free the pad for someone else."""
    scheduler = FacilityScheduler([make_profile("hub-stale", pads=1, charger_bays=2)])
    granted = scheduler.propose(make_request("req-granted", "hub-stale", deadline_s=100))
    assert granted.granted
    competitor = scheduler.propose(
        make_request("req-competitor", "hub-stale", deadline_s=100, allow_queue=False)
    )
    assert competitor.plan is None
    assert scheduler.audit("hub-stale").pads.peak_overlap == 1


def test_policy_version_mismatch_is_refused_not_guessed() -> None:
    scheduler = FacilityScheduler([make_profile("hub-policy")])
    decision = scheduler.propose(make_request("req-policy", "hub-policy", policy_version="other-v9"))
    assert decision.reason is WaitReason.POLICY_VERSION_MISMATCH
    assert decision.plan is None


def test_unknown_facility_is_a_reason_not_an_exception() -> None:
    scheduler = FacilityScheduler([make_profile("hub-known")])
    decision = scheduler.propose(make_request("req-ghost", "hub-unknown"))
    assert decision.reason is WaitReason.NO_SUCH_FACILITY
    assert decision.state is DecisionState.QUEUED


def test_a_request_carrying_its_own_durations_uses_them() -> None:
    heavy = TurnaroundDurations(
        approach_us=10 * SECOND_US,
        landing_us=10 * SECOND_US,
        handover_us=40 * SECOND_US,
        loading_us=120 * SECOND_US,
        departure_us=10 * SECOND_US,
    )
    plan = FacilityScheduler([make_profile("hub-heavy")]).propose(
        make_request("req-heavy", "hub-heavy", durations=heavy, requires_recharge=False)
    ).plan
    assert plan is not None
    assert plan.end_us - plan.start_us == heavy.total_us == 190 * SECOND_US
    assert plan.stages[-1].window.measure == heavy.departure_us


# --- repositioning: data for the dispatcher --------------------------------


def test_alternate_site_quotes_describe_but_never_choose() -> None:
    origin = make_profile("hub-slow", pads=1, charger_bays=1)
    other = make_profile("node-fast", pads=3, charger_bays=2)
    scheduler = FacilityScheduler([origin, other])
    blocked = scheduler.propose(make_request("req-1", "hub-slow"))
    scheduler.propose(make_request("req-2", "hub-slow", deadline_s=100, allow_queue=False))
    assert blocked.granted

    quotes = alternate_site_quotes(
        "req-2", origin, (origin, other), WaitReason.PAD_WAIT, scheduler.occupied, at_us=0
    )
    assert [quote.target_facility_id for quote in quotes] == ["node-fast"]
    quote = quotes[0]
    assert quote.comparable and quote.target.pads_free == 3
    assert quote.priced_by == "dispatcher"
    assert quote.decides_nothing is True
    assert quote.as_metrics()["leg_authority"] == "corridor_graph_and_energy"
    # No cost, duration or ranking is expressed by this package.
    assert not hasattr(quote, "cost") and not hasattr(quote, "rank")
    assert not hasattr(quote, "recommended")


def test_incomparable_sites_are_marked_not_hidden() -> None:
    origin = make_profile("hub-home")
    foreign = make_profile("hub-foreign", policy_version="sim-facility-policy-v9")
    scheduler = FacilityScheduler([origin, foreign])
    quotes = alternate_site_quotes(
        "req-x", origin, (origin, foreign), WaitReason.PAD_WAIT, scheduler.occupied, at_us=0
    )
    assert [(quote.target_facility_id, quote.excluded_reason) for quote in quotes] == [
        ("hub-foreign", "policy_version")
    ]


def test_availability_counts_only_declared_units_at_the_instant() -> None:
    from droneops_facilities import availability_at

    profile = make_profile("hub-avail", pads=2, charger_bays=2, crew_slots=1)
    busy = {"pad:pad-hub-avail-0": (Interval.of(0, 100 * SECOND_US),)}
    counts = availability_at(profile, lambda token: busy.get(token, ()), at_us=50 * SECOND_US)
    assert counts.pads_free == 1 and counts.chargers_free == 2 and counts.crew_free is True
    assert counts.can_start_something


def test_overlap_count_treats_adjacency_as_not_overlap() -> None:
    assert overlap_count((Interval.of(0, 5), Interval.of(5, 9))) == 1
    assert overlap_count((Interval.of(0, 9), Interval.of(5, 9))) == 2
    assert overlap_count(()) == 0
    assert overlap_count((Interval.of(3, 3),)) == 0


def test_resource_unit_identity_matches_the_planner_naming() -> None:
    """Facility keys must name the objects the planner already reads occupancy for."""
    units = make_profile("hub-central-synthetic").pads()
    assert units[0].identifier == "pad-hub-central-synthetic-0"
    assert make_profile("hub-central-synthetic").chargers()[0].identifier == (
        "charger-hub-central-synthetic-0"
    )
    assert units[0].kind is ResourceKind.PAD
