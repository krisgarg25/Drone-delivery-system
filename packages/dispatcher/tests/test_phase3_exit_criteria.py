"""Phase 3 exit criteria, one named test each.

These are the bullets of ``technical-build/05-implementation-phases.md`` "Phase 3:
exit criteria", the invariants ``I4``, ``I5`` and ``I6`` from
``technical-build/06-testing-and-safety.md`` section 2, and the three dispatch
scenarios from its section 6. Each test asserts the *invariant*, not the function:
I5 in particular is checked against the candidate query, because "not assigned" is
equally true of an implementation that merely ranked an offline aircraft last.
"""

from __future__ import annotations

import dataclasses
import os
import subprocess
import sys
from collections.abc import Sequence

import dispatch_support as sup
import pytest

from droneops_contracts import (
    AircraftStatus,
    FacilityStatus,
    HandoverType,
    LinkQuality,
    MissionStatus,
    PriorityClass,
)
from droneops_dispatcher import (
    AIRBORNE_STATUSES,
    ASSIGNABLE_STATES,
    EMERGENCY_RIGHTS,
    ONE_SOLVER_WORKER,
    OPERABLE_FLOOR,
    AcceptanceToken,
    AirborneChangeProposal,
    AlternativeOption,
    AppendOnlyDecisionLog,
    AssignmentRequest,
    ChangePreview,
    DecisionOutcome,
    DispatchableMission,
    Dispatcher,
    DispatchPolicy,
    DispatchQueue,
    FacilityWaits,
    IneligibleReason,
    MissionLegs,
    MissionStatusRow,
    PriorityRight,
    ProposalState,
    ReasonCode,
    RepositioningCost,
    RepositioningLeg,
    SafetyParameters,
    SolverDeterminismError,
    SolverSettings,
    airborne_change_authority,
    best_repositioning,
    candidate_query,
    draft_airborne_change,
    median_low,
    powers_of,
    price_repositioning,
    rank_scores,
    score_candidate,
    screen_fleet,
    solve_assignment,
    withdrawal_is_legal,
)
from droneops_facilities import (
    Accessibility,
    CapacityProfile,
    ClaimPurpose,
    FacilityScheduler,
    TurnaroundDurations,
    TurnaroundRequest,
    WaitReason,
    alternate_site_quotes,
    overlap_count,
)
from droneops_geom_core import Interval
from droneops_platform import AppendOnlyViolation

MINUTE_US = sup.MINUTE_US
pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- I5
def test_i5_offline_and_maintenance_aircraft_are_never_eligible() -> None:
    """The candidate query omits them; it does not rank them last."""
    fleet = (
        sup.aircraft("ac-fit", charge_bp=10_000),
        sup.aircraft("ac-offline", status=AircraftStatus.OFFLINE),
        sup.aircraft("ac-maintenance", status=AircraftStatus.MAINTENANCE),
        sup.aircraft("ac-retired", status=AircraftStatus.RETIRED),
        sup.aircraft("ac-contingency", status=AircraftStatus.CONTINGENCY),
    )
    query = candidate_query(fleet, policy=sup.policy_with(), as_of_us=0, payload_g=500)

    assert "ac-offline" not in query.candidate_ids
    assert "ac-maintenance" not in query.candidate_ids
    # The stronger assertion I5 asks for: nothing outside the assignable set
    # reached the candidate list at all, so no ranking step could have rescued it.
    query.assert_candidates_are(["ac-fit"])
    assert {row.reason for row in query.excluded} >= {
        IneligibleReason.OFFLINE,
        IneligibleReason.MAINTENANCE,
        IneligibleReason.RETIRED,
        IneligibleReason.CONTINGENCY,
    }
    assert ASSIGNABLE_STATES <= OPERABLE_FLOOR


def test_i5_an_excluded_aircraft_cannot_re_enter_through_the_energy_screen() -> None:
    fleet = (
        sup.aircraft("ac-fit", charge_bp=10_000),
        sup.aircraft("ac-offline", status=AircraftStatus.OFFLINE),
        sup.aircraft("ac-maintenance", status=AircraftStatus.MAINTENANCE),
    )
    policy = sup.policy_with()
    query = candidate_query(fleet, policy=policy, as_of_us=0)
    screens = screen_fleet(sup.legs("m-1"), sup.weather(), query.candidates, clock_us=0)
    assert {row.aircraft_id for row in screens.rows} == {"ac-fit"}
    assert protected_but_not_offered(query.candidate_ids)


def protected_but_not_offered(candidate_ids: frozenset[str]) -> bool:
    return "ac-offline" not in candidate_ids and "ac-maintenance" not in candidate_ids


# --------------------------------------------------------------------------- I4
def test_i4_no_mission_is_assigned_to_an_aircraft_that_cannot_complete_recovery_with_reserve() -> (
    None
):
    """Only the tail that can fly out, hand over, recover and keep reserve is chosen."""
    hub, other = _hubs()
    profiles = (hub, other)
    as_of_us = hub.opens_us + 10 * MINUTE_US
    fleet = (
        sup.aircraft("ac-flat", charge_bp=1500, as_of_us=as_of_us),
        sup.aircraft("ac-fit", charge_bp=10_000, as_of_us=as_of_us),
    )
    log = AppendOnlyDecisionLog()
    dispatcher = Dispatcher(
        policy=sup.policy_with(),
        fleet=fleet,
        scheduler=FacilityScheduler(profiles),
        profiles=profiles,
        log=log,
    )
    result = dispatcher.run_round(
        (_dispatchable("m-i4", hub, as_of_us, payload_g=900, long_route=True),),
        as_of_us=as_of_us,
        inputs=sup.round_inputs(profiles),
        round_id="r-i4",
    )

    assert result.outcomes[0].outcome is DecisionOutcome.ASSIGNED
    assert result.outcomes[0].aircraft_id == "ac-fit"
    decision = log.latest("m-i4")
    assert decision is not None
    assert [row[0] for row in decision.ranked] == ["ac-fit"]
    energy = [row for row in decision.rejections if row.code is ReasonCode.ENERGY_RESERVE_SHORT]
    assert energy, "the flat aircraft must be refused for energy, with the margin named"
    assert energy[0].detail_of("margin_wh")
    assert energy[0].detail_of("breaking_term")
    assert energy[0].subject_id == "ac-flat"


def test_i4_a_mission_no_aircraft_can_recover_is_rejected_not_invented() -> None:
    hub, other = _hubs()
    profiles = (hub, other)
    as_of_us = hub.opens_us + MINUTE_US
    fleet = (sup.aircraft("ac-flat", charge_bp=800, as_of_us=as_of_us),)
    log = AppendOnlyDecisionLog()
    dispatcher = Dispatcher(
        policy=sup.policy_with(),
        fleet=fleet,
        scheduler=FacilityScheduler(profiles),
        profiles=profiles,
        log=log,
    )
    as_of_us = hub.opens_us + MINUTE_US
    result = dispatcher.run_round(
        (_dispatchable("m-impossible", hub, as_of_us, long_route=True),),
        as_of_us=as_of_us,
        inputs=sup.round_inputs(profiles),
        round_id="r-impossible",
    )
    assert result.outcomes[0].outcome is DecisionOutcome.REJECTED
    decision = log.latest("m-impossible")
    assert decision is not None
    assert decision.rejections
    assert {row.code for row in decision.rejections} & {
        ReasonCode.ENERGY_RESERVE_SHORT,
        ReasonCode.RECOVERY_INFEASIBLE,
    }


# --------------------------------------------------------------------------- I6
def test_i6_full_pads_and_chargers_queue_rather_than_overbook() -> None:
    """Declared capacity is the ceiling: work waits, is refused, or is sequenced - never doubled."""
    hub, other = _hubs(pads=1, crew_slots=1, charger_bays=1)
    ready = hub.opens_us + 2 * MINUTE_US
    requests = tuple(
        TurnaroundRequest(
            request_id=f"turn-{index}",
            facility_id=hub.facility_id,
            holder=f"m-{index}",
            ready_us=ready,
            deadline_us=hub.closes_us - MINUTE_US,
            policy_version=sup.FACILITY_POLICY,
        )
        for index in range(4)
    )
    batch = FacilityScheduler((hub, other)).schedule(requests)
    assert batch.i6_holds, "no unit may hold two claims at once"
    pad_windows = [
        claim.window for plan in batch.scheduled for claim in plan.claims_for(ClaimPurpose.PAD)
    ]
    assert overlap_count(pad_windows) <= hub.pad_count
    assert batch.metrics["queued"] + batch.metrics["rejected"] + len(batch.scheduled) == 4
    assert int(batch.metrics["wait_us_pad_wait"]) > 0, "contention must be attributed as a wait"

    # Same four requests, but each needs to be finished inside three minutes: the
    # horizon cannot hold them, so the answer is a queue or a refusal - not a
    # second claim on one pad.
    tight = tuple(
        dataclasses.replace(request, deadline_us=ready + 3 * MINUTE_US) for request in requests
    )
    squeezed = FacilityScheduler((hub, other)).schedule(tight)
    assert squeezed.i6_holds
    assert len(squeezed.scheduled) < 4
    assert squeezed.queued or squeezed.rejected
    assert len(squeezed.scheduled) + len(squeezed.queued) + len(squeezed.rejected) == 4


def test_i6_a_dispatch_round_never_runs_two_hub_turns_in_one_slot() -> None:
    hub, other = _hubs(pads=1, crew_slots=1, charger_bays=1)
    profiles = (hub, other)
    as_of_us = hub.opens_us + 2 * MINUTE_US
    fleet = tuple(sup.aircraft(f"ac-{letter}", as_of_us=as_of_us) for letter in ("a", "b", "c"))
    dispatcher = Dispatcher(
        policy=sup.policy_with(),
        fleet=fleet,
        scheduler=FacilityScheduler(profiles),
        profiles=profiles,
    )
    missions = tuple(
        _dispatchable(f"m-{index}", hub, as_of_us, legs_of=sup.legs(f"m-{index}"))
        for index in range(3)
    )
    result = dispatcher.run_round(
        missions, as_of_us=as_of_us, inputs=sup.round_inputs(profiles), round_id="r-i6"
    )
    assigned = result.of(DecisionOutcome.ASSIGNED)
    assert len(assigned) + len(result.of(DecisionOutcome.QUEUED)) + len(
        result.of(DecisionOutcome.REJECTED)
    ) == len(missions)
    # Declared capacity bounds *simultaneity*: one crew slot may run turns in
    # sequence, but never two at once, and never by double-booking the slot.
    starts = [row.start_us for row in assigned]
    windows = [Interval.of(start, start + 4 * MINUTE_US) for start in starts]
    assert overlap_count(windows) <= hub.crew_slots
    assert len(windows) == len(set(starts)), "two turns may not share a start in one slot"
    assert result.metric("crew_wait_us") is not None


def test_i6_the_solver_refuses_to_start_more_turns_than_a_hub_has_crew_for() -> None:
    policy = sup.policy_with()
    requests = _crew_requests(4)
    one_crew = solve_assignment(requests, policy=policy, crew_capacity={"hub-a": 1}, round_id="r")
    two_crew = solve_assignment(requests, policy=policy, crew_capacity={"hub-a": 2}, round_id="r")
    assert len(one_crew.assignments) == 1
    assert len(two_crew.assignments) == 2
    assert one_crew.num_search_workers == ONE_SOLVER_WORKER


def _crew_requests(count: int) -> Sequence[AssignmentRequest]:
    return tuple(
        AssignmentRequest(
            mission_id=f"m-{index}",
            aircraft_id=f"ac-{index}",
            units=index,
            earliest_start_us=0,
            latest_start_us=10 * MINUTE_US,
            duration_us=12 * MINUTE_US,
            hub_facility_id="hub-a",
            tie_index=index,
        )
        for index in range(count)
    )


# ---------------------------------------------------------------- priority
def test_priority_cannot_delete_an_airborne_commitment() -> None:
    """Resequencing is the whole of what priority may do. Deletion is not in it."""
    for priority in PriorityClass:
        airborne = MissionStatusRow(
            mission_id="m-air",
            priority=PriorityClass.ROUTINE,
            status=MissionStatus.IN_PROGRESS,
            aircraft_id="ac-03",
        )
        assert airborne.status in AIRBORNE_STATUSES
        assert not withdrawal_is_legal(airborne), (
            "the mission state machine cannot express cancelling an in-progress mission"
        )
        queue = DispatchQueue(policy=sup.policy_with())
        queue.enqueue(
            sup.mission(
                "m-air",
                priority=airborne.priority,
                status=MissionStatus.IN_PROGRESS,
                aircraft_id="ac-03",
            ).to_queued()
        )
        queue.enqueue(sup.mission("m-ground").to_queued())
        arriving = sup.mission("m-arriving", priority=priority).to_queued()
        outcome = queue.resequence(arriving, now_us=10 * MINUTE_US)

        assert queue.get("m-air") is not None, f"{priority.value} removed an airborne mission"
        assert len(queue) == 3, f"{priority.value} deleted a mission"
        assert queue.order(10 * MINUTE_US)[0].mission_id == "m-air", "the pinned index holds"
        assert outcome.pinned
        assert outcome.pinned[0][0] == "m-air"
        assert any(row.target_mission_id == "m-air" for row in outcome.refused)
        assert "m-ground" in queue.ids
        assert queue.get("m-arriving") is not None


def test_priority_resequences_unlaunched_missions_from_the_safety_scenario() -> None:
    case = sup.mission_scenario("priority-arrival")
    given = dict(case["machine_input"])
    expected = dict(case["expected_metrics"])
    routine = dict(given["routine_mission"])
    second = dict(given["second_routine"])
    arriving = dict(given["arriving"])
    policy = sup.policy_with(
        starvation_bound_us=int(given["starvation_bound_us"]),
        aging_step_us=int(given["aging_step_us"]),
    )
    queue = DispatchQueue(policy=policy)
    for row in (routine, second):
        queue.enqueue(
            sup.mission(
                str(row["mission_id"]),
                priority=PriorityClass(str(row["priority"])),
                arrived_at_us=int(row["arrived_at_us"]),
                deadline_us=int(row["deadline_us"]),
            ).to_queued()
        )
    now_us = int(arriving["arrived_at_us"])
    outcome = queue.resequence(
        sup.mission(
            str(arriving["mission_id"]),
            priority=PriorityClass(str(arriving["priority"])),
            arrived_at_us=now_us,
            deadline_us=int(arriving["deadline_us"]),
        ).to_queued(),
        now_us=now_us,
    )
    order = queue.order(now_us)
    assert order[0].mission_id == str(expected["head_mission"])
    assert len(order) == int(expected["queue_length_after"])
    assert int(expected["missions_deleted"]) == 0
    assert {row.mission_id for row in order} == {"m-routine", "m-routine-2", "m-medlab"}
    assert outcome.order_before == ("m-routine", "m-routine-2")
    assert queue.get("m-routine") is not None, "resequenced, never removed"
    assert not outcome.refused, "unlaunched work may be reordered"


def test_starvation_is_bounded_at_the_named_bound() -> None:
    bound = 15 * MINUTE_US
    policy = sup.policy_with(starvation_bound_us=bound, aging_step_us=3 * MINUTE_US)
    queue = DispatchQueue(policy=policy)
    routine = sup.mission("m-routine", arrived_at_us=0, deadline_us=120 * MINUTE_US).to_queued()
    queue.enqueue(routine)
    for index in range(5):
        queue.enqueue(
            sup.mission(
                f"m-sla-{index}",
                priority=PriorityClass.COMMERCIAL_SLA,
                arrived_at_us=MINUTE_US,
                deadline_us=(30 + index) * MINUTE_US,
            ).to_queued()
        )

    just_short = queue.fairness(routine, bound - 1)
    at_bound = queue.fairness(routine, bound)
    assert not just_short.starved
    assert not just_short.entitled
    assert at_bound.starved
    assert at_bound.entitled, "the bound is inclusive by definition"
    assert at_bound.waited_us == bound
    assert at_bound.bound_us == bound
    assert queue.order(bound - 1)[0].mission_id == "m-sla-0"
    assert queue.order(bound)[0].mission_id == "m-routine", "at the bound it goes first"
    events = queue.starved(bound)
    assert [row.mission_id for row in events] == ["m-routine"]
    assert events[0].explanation(PriorityClass.ROUTINE).count("starvation_bound_us") == 1
    later = queue.starved(bound + MINUTE_US)
    assert [row.mission_id for row in later] == [f"m-sla-{index}" for index in range(5)]
    assert queue.starved(bound + 2 * MINUTE_US) == (), "one event per mission, not per round"


def test_aging_never_lifts_routine_work_over_life_critical() -> None:
    policy = sup.policy_with(starvation_bound_us=15 * MINUTE_US, aging_step_us=MINUTE_US)
    queue = DispatchQueue(policy=policy)
    routine = sup.mission("m-old", arrived_at_us=0, deadline_us=600 * MINUTE_US).to_queued()
    queue.enqueue(routine)
    rank = queue.fairness(routine, 600 * MINUTE_US)
    assert rank.effective_rank == 2, "aging stops at the ordinary band, below life-critical"
    assert rank.aging_steps == 1, "aging is capped at the distance to the band floor"
    assert rank.entitled
    assert rank.starved


def test_commercial_sla_can_never_buy_an_emergency_right_of_way() -> None:
    sla = powers_of(PriorityClass.COMMERCIAL_SLA)
    life = powers_of(PriorityClass.LIFE_CRITICAL)
    safety = powers_of(PriorityClass.SAFETY_CONTINGENCY)
    assert not sla.has_emergency_right_of_way()
    assert not (sla.rights & EMERGENCY_RIGHTS)
    assert life.allows(PriorityRight.PROPOSE_AIRBORNE_CHANGE)
    assert safety.allows(PriorityRight.SKIP_FAIRNESS_AGING)
    assert not life.allows(PriorityRight.SKIP_FAIRNESS_AGING)
    assert sla.allows(PriorityRight.RESEQUENCE_UNLAUNCHED)

    target = MissionStatusRow(
        mission_id="m-air",
        priority=PriorityClass.ROUTINE,
        status=MissionStatus.ACTIVATED,
        aircraft_id="ac-03",
    )
    sla_mission = MissionStatusRow(
        mission_id="m-sla", priority=PriorityClass.COMMERCIAL_SLA, status=MissionStatus.ELIGIBLE
    )
    refusal = airborne_change_authority(sla_mission)
    assert refusal is not None
    assert refusal.code == "no_airborne_change_right"
    proposal, drafted = draft_airborne_change(
        preview=_preview("m-sla"), target=target, requesting=sla_mission, policy=sup.policy_with()
    )
    assert proposal is None
    assert drafted is not None
    life_mission = MissionStatusRow(
        mission_id="m-med", priority=PriorityClass.LIFE_CRITICAL, status=MissionStatus.ELIGIBLE
    )
    assert airborne_change_authority(life_mission) is None


def test_an_airborne_change_is_a_proposal_and_decline_or_expiry_keeps_the_commitment() -> None:
    case = sup.mission_scenario("priority-airborne-request")
    given = dict(case["machine_input"])
    operator = dict(given["operator"])
    expires_at_us = int(given["preview_expires_at_us"])
    target = MissionStatusRow(
        mission_id="m-air",
        priority=PriorityClass.ROUTINE,
        status=MissionStatus.ACTIVATED,
        aircraft_id=str(dict(given["airborne"])["aircraft_id"]),
    )
    requesting = MissionStatusRow(
        mission_id="m-med",
        priority=PriorityClass.LIFE_CRITICAL,
        status=MissionStatus.ELIGIBLE,
    )
    proposal = AirborneChangeProposal(
        preview=_preview("m-med", expires_at_us=expires_at_us),
        target=target,
        requesting=requesting,
        policy=sup.policy_with(),
    )
    assert proposal.state is ProposalState.PENDING
    assert proposal.commitment_intact
    assert not proposal.command_issued

    with pytest.raises(Exception, match="second_factor"):
        proposal.accept(
            AcceptanceToken("op-1", "pilot", "reviewed", False, "tok-unsigned"), at_us=MINUTE_US
        )
    token = AcceptanceToken(
        str(operator["identity"]),
        str(operator["role"]),
        str(operator["reason"]),
        bool(operator["second_factor"]),
        "tok-1",
    )
    accepted = proposal.accept(token, at_us=2 * MINUTE_US)
    assert accepted.commitment_intact is False
    assert accepted.command_issued is False
    assert proposal.accept(token, at_us=3 * MINUTE_US).digest() == accepted.digest(), "idempotent"
    other = AcceptanceToken("op-9", "pilot", "me too", True, "tok-9")
    with pytest.raises(Exception, match="accepted"):
        proposal.accept(other, at_us=4 * MINUTE_US)

    declined = AirborneChangeProposal(
        preview=_preview("m-med-b", expires_at_us=expires_at_us),
        target=target,
        requesting=requesting,
        policy=sup.policy_with(),
    )
    refused = declined.decline(
        AcceptanceToken("op-2", "flight_operations_officer", "not safe today", True, "tok-2"),
        at_us=MINUTE_US,
    )
    assert refused.commitment_intact is True
    assert declined.commitment_intact is True

    lapsed = AirborneChangeProposal(
        preview=_preview("m-med-c", expires_at_us=expires_at_us),
        target=target,
        requesting=requesting,
        policy=sup.policy_with(),
    )
    assert lapsed.expire(expires_at_us - MINUTE_US) is None, "not due yet"
    expiry = lapsed.expire(expires_at_us + MINUTE_US)
    assert expiry is not None
    assert expiry.state is ProposalState.EXPIRED
    assert lapsed.commitment_intact is True
    assert int(dict(case["expected_metrics"])["missions_deleted"]) == 0


def test_battery_shortfall_scenario_rejects_and_finds_the_alternate_aircraft() -> None:
    case = sup.mission_scenario("battery-shortfall")
    given = dict(case["machine_input"])
    expected = dict(case["expected_metrics"])
    shape = dict(given["mission"])
    hub, other = _hubs()
    profiles = (hub, other)
    as_of_us = hub.opens_us + 10 * MINUTE_US
    fleet = tuple(
        sup.aircraft(
            str(row["aircraft_id"]),
            status=AircraftStatus(str(row["status"])),
            charge_bp=int(row["charge_bp"]),
            as_of_us=as_of_us,
        )
        for row in given["fleet"]
    )
    log = AppendOnlyDecisionLog()
    dispatcher = Dispatcher(
        policy=sup.policy_with(),
        fleet=fleet,
        scheduler=FacilityScheduler(profiles),
        profiles=profiles,
        log=log,
    )
    result = dispatcher.run_round(
        (
            _dispatchable(
                "m-shortfall",
                hub,
                as_of_us,
                legs_of=MissionLegs(
                    mission_id="m-shortfall",
                    payload_g=int(shape["payload_g"]),
                    outbound_mm=int(shape["outbound_mm"]),
                    handover_mm=int(shape["handover_mm"]),
                    recovery_mm=int(shape["recovery_mm"]),
                    climb_mm=int(shape["climb_mm"]),
                ),
                turnaround_us=int(shape["turnaround_us"]),
            ),
        ),
        as_of_us=as_of_us,
        inputs=sup.round_inputs(profiles),
        round_id="r-shortfall",
    )
    assigned = result.of(DecisionOutcome.ASSIGNED)
    assert len(assigned) == int(expected["assigned"])
    assert assigned[0].aircraft_id == str(expected["assigned_aircraft"])
    decision = log.latest("m-shortfall")
    assert decision is not None
    exclusions = {
        row.code
        for row in decision.rejections
        if row.code in (ReasonCode.OFFLINE, ReasonCode.MAINTENANCE)
    }
    assert len(exclusions) == 2
    assert {
        row.subject_id for row in decision.rejections if row.code is ReasonCode.ENERGY_RESERVE_SHORT
    } == {"ac-flat"}


# ------------------------------------------------------------------- determinism
def test_assignment_is_deterministic_under_a_fixed_seed_and_one_worker() -> None:
    requests = _solver_requests()
    policy = sup.policy_with()
    first = solve_assignment(requests, policy=policy, crew_capacity={"hub-a": 2}, round_id="r")
    second = solve_assignment(requests, policy=policy, crew_capacity={"hub-a": 2}, round_id="r")
    assert first.status is second.status
    assert first.ranking_digest == second.ranking_digest
    assert first.digest() == second.digest()
    assert first.is_reproducible_with(second)
    assert first.num_search_workers == ONE_SOLVER_WORKER == 1
    assert first.random_seed == policy.solver.random_seed
    assert first.solver_settings_digest == policy.solver.digest()
    assert dict(first.determinism)["num_search_workers"].startswith("1")


def test_a_parallel_solver_is_refused_before_it_can_break_replay() -> None:
    with pytest.raises(SolverDeterminismError):
        SolverSettings(num_search_workers=4)
    with pytest.raises(SolverDeterminismError):
        sup.policy_with(solver=SolverSettings(num_search_workers=2))
    with pytest.raises(SolverDeterminismError):
        SolverSettings(random_seed=-1)


def test_the_ranking_is_byte_identical_across_pythonhashseed_values() -> None:
    """A fresh interpreter per seed: an in-process test cannot prove this one."""
    script = _hashseed_script()
    observed: list[str] = []
    for seed in ("0", "1", "7", "12345"):
        env = dict(os.environ, PYTHONHASHSEED=seed)
        completed = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            env=env,
            check=True,
        )
        observed.append(completed.stdout.strip())
    assert len(set(observed)) == 1, f"output changed with PYTHONHASHSEED: {observed}"
    assert observed[0].startswith("sha256:")


# ------------------------------------------------------------------ rejection etc
def test_every_rejection_carries_a_human_reason_and_technical_detail() -> None:
    hub, other = _hubs()
    profiles = (hub, other)
    as_of_us = hub.opens_us + MINUTE_US
    fleet = (
        sup.aircraft("ac-offline", status=AircraftStatus.OFFLINE, as_of_us=as_of_us),
        sup.aircraft("ac-maintenance", status=AircraftStatus.MAINTENANCE, as_of_us=as_of_us),
        sup.aircraft("ac-lost-link", link=LinkQuality.LOST, as_of_us=as_of_us),
    )
    log = AppendOnlyDecisionLog()
    dispatcher = Dispatcher(
        policy=sup.policy_with(),
        fleet=fleet,
        scheduler=FacilityScheduler(profiles),
        profiles=profiles,
        log=log,
    )
    dispatcher.run_round(
        (_dispatchable("m-none", hub, as_of_us),),
        as_of_us=as_of_us,
        inputs=sup.round_inputs(profiles),
        round_id="r-none",
    )
    decision = log.latest("m-none")
    assert decision is not None
    assert decision.outcome is DecisionOutcome.REJECTED
    assert decision.rejections, "a rejection without a reason is a bug"
    codes = {row.code for row in decision.rejections}
    assert {ReasonCode.OFFLINE, ReasonCode.MAINTENANCE, ReasonCode.LINK_UNCERTAIN} <= codes
    assert ReasonCode.NO_ELIGIBLE_AIRCRAFT in codes
    for row in decision.rejections:
        assert row.human_reason.strip()
        assert row.technical, "the console needs a drawer, not only a sentence"
        assert row.persisted
        assert row.banner.startswith("Simulation only")
        assert row.policy_version
        assert row.config_release_sha
    assert decision.human_summary()[-1].startswith("Simulation only")
    assert decision.as_document()["rejections"][0]["technical_detail"]


def test_the_decision_log_is_append_only_and_reads_back_every_reason() -> None:
    log = AppendOnlyDecisionLog()
    hub, other = _hubs()
    profiles = (hub, other)
    as_of_us = hub.opens_us + MINUTE_US
    fleet = (sup.aircraft("ac-fit", as_of_us=as_of_us),)
    dispatcher = Dispatcher(
        policy=sup.policy_with(),
        fleet=fleet,
        scheduler=FacilityScheduler(profiles),
        profiles=profiles,
        log=log,
    )
    first = dispatcher.run_round(
        (_dispatchable("m-log", hub, as_of_us),),
        as_of_us=as_of_us,
        inputs=sup.round_inputs(profiles),
        round_id="r-log",
    )
    assert len(log) == 1
    assert log.append(first.outcomes[0].decision) is first.outcomes[0].decision, "idempotent"
    assert len(log) == 1, "a re-append of the same record is not a second record"
    tampered = first.outcomes[0].decision.__class__(
        **{**first.outcomes[0].decision.__dict__, "queue_state": "rewritten"}
    )
    with pytest.raises(AppendOnlyViolation):
        log.append(tampered)
    assert log.latest("m-log") is not None
    assert "update" not in [name for name in dir(log) if not name.startswith("_")]


def test_repositioning_is_priced_from_the_quotes_facilities_hands_over() -> None:
    policy = sup.policy_with()
    origin, target = _hubs(pads=2, crew_slots=2, charger_bays=2)
    profiles = (origin, target)
    scheduler = FacilityScheduler(profiles)
    quotes = alternate_site_quotes(
        "m-1@ac-a",
        origin,
        (target,),
        WaitReason.PAD_WAIT,
        scheduler.occupied,
        at_us=origin.opens_us + MINUTE_US,
        origin_wait_us=10 * MINUTE_US,
    )
    assert all(quote.priced_by == "dispatcher" for quote in quotes)
    priced = price_repositioning(
        quotes,
        (RepositioningLeg(target_facility_id=target.facility_id, leg_mm=8_000_000),),
        policy=policy,
        cruise_power_w=200,
        reference_airspeed_dmps=150,
    )
    assert priced, "the dispatcher must answer the priced_by=dispatcher handoff"
    chosen: RepositioningCost | None = best_repositioning(priced)
    assert chosen is not None
    assert chosen.energy_wh > 0
    assert chosen.leg_us > 0
    assert chosen.units != 0
    assert chosen.priced_by == "dispatcher"
    assert chosen.leg_mm == 8_000_000
    unpriced = price_repositioning(
        quotes, (), policy=policy, cruise_power_w=200, reference_airspeed_dmps=150
    )
    assert all(row.excluded_reason == "no_leg_geometry" for row in unpriced)
    assert best_repositioning(unpriced) is None, "no geometry, no guess"
    assert median_low([5, 1, 3]) == 3


def test_ranking_is_a_total_order_and_the_policy_must_be_versioned() -> None:
    with pytest.raises(Exception, match="starvation_bound_us"):
        DispatchPolicy(starvation_bound_us=0)
    with pytest.raises(Exception, match="version"):
        DispatchPolicy(policy_version="  ")
    with pytest.raises(Exception, match="aging"):
        DispatchPolicy(aging_step_us=0)
    policy = sup.policy_with()
    fleet = (sup.aircraft("ac-b", charge_bp=9000), sup.aircraft("ac-a", charge_bp=9500))
    screens = screen_fleet(
        sup.legs("m-1"),
        sup.weather(),
        candidate_query(fleet, policy=policy, as_of_us=0).candidates,
        clock_us=0,
    )
    rows = [
        score_candidate(
            mission_id="m-1",
            aircraft_id=screen.aircraft_id,
            margin_wh=screen.margin_wh,
            earliest_start_us=0,
            duration_us=5 * MINUTE_US,
            deadline_us=30 * MINUTE_US,
            waits=FacilityWaits(pad_wait_us=MINUTE_US),
            policy=policy,
        )
        for screen in screens.feasible
    ]
    ranked = rank_scores(rows)
    assert [row.units for row in ranked] == sorted(row.units for row in ranked)
    assert len({row.aircraft_id for row in ranked}) == len(ranked)


# ------------------------------------------------------------------------- helpers
def _hubs(
    *, pads: int = 2, crew_slots: int = 1, charger_bays: int = 1
) -> tuple[CapacityProfile, CapacityProfile]:
    open_us = 0
    close_us = 8 * 60 * MINUTE_US
    return (
        _profile(
            "hub-a",
            pads=pads,
            crew_slots=crew_slots,
            charger_bays=charger_bays,
            opens_us=open_us,
            closes_us=close_us,
        ),
        _profile(
            "hub-b",
            pads=2,
            crew_slots=2,
            charger_bays=2,
            opens_us=open_us,
            closes_us=close_us,
        ),
    )


def _profile(
    facility_id: str,
    *,
    pads: int,
    crew_slots: int,
    charger_bays: int,
    opens_us: int,
    closes_us: int,
) -> CapacityProfile:
    return CapacityProfile(
        facility_id=facility_id,
        kind="hub",
        name=f"synthetic {facility_id}",
        status=FacilityStatus.ACTIVE,
        opens_us=opens_us,
        closes_us=closes_us,
        pad_count=pads,
        charger_bays=charger_bays,
        crew_slots=crew_slots,
        turnaround=TurnaroundDurations.from_minutes(loading_min=3, offload_min=2),
        accessibility=Accessibility(
            handover_type=HandoverType.LAND_TRANSFER,
            step_free=True,
            ground_pickup=False,
            requires_crew=True,
        ),
        charge_power_w=1200,
        crew_shifts=(Interval.of(opens_us, closes_us),),
        policy_version=sup.FACILITY_POLICY,
    )


def _dispatchable(
    mission_id: str,
    hub: CapacityProfile,
    as_of_us: int,
    *,
    payload_g: int = 500,
    long_route: bool = False,
    legs_of: MissionLegs | None = None,
    turnaround_us: int = 4 * MINUTE_US,
) -> DispatchableMission:
    shape = legs_of or sup.legs(
        mission_id,
        payload_g=payload_g,
        outbound_mm=12_000_000 if long_route else 2_000_000,
        recovery_mm=9_000_000 if long_route else 1_000_000,
    )
    return DispatchableMission(
        mission_id=mission_id,
        order_id=f"ord-{mission_id}",
        priority=PriorityClass.ROUTINE,
        status=MissionStatus.ELIGIBLE,
        arrived_at_us=as_of_us,
        deadline_us=as_of_us + 40 * MINUTE_US,
        legs=shape,
        origin_facility_id=hub.facility_id,
        turnaround_us=turnaround_us,
    )


def _preview(mission_id: str, *, expires_at_us: int = 20 * MINUTE_US) -> ChangePreview:
    return ChangePreview(
        proposal_id=f"prop-{mission_id}",
        mission_id=mission_id,
        aircraft_id="ac-03",
        plan_and_config_hash="sha256:" + "f" * 63,
        affected_resources=(("pad-hub-a-0", 0, MINUTE_US),),
        expected_delay_us=6 * MINUTE_US,
        energy_margin_wh=42,
        alternatives=(
            AlternativeOption(
                label="hold and re-plan after landing",
                aircraft_id=None,
                expected_delay_us=14 * MINUTE_US,
                energy_margin_wh=120,
                units=900,
                note="no airborne change",
            ),
        ),
        affected_pilots=(("pilot-7", "remote pilot"),),
        safety_parameters=SafetyParameters(
            policy_version="sim-policy-v1",
            config_release_sha="sha256:" + "ab" * 32,
            horizontal_margin_mm=15_000,
            vertical_margin_mm=10_000,
            timing_uncertainty_us=5 * MINUTE_US,
            energy_basis="airframe-x-sim@illustrative-1 (illustrative)",
        ),
        consequences=(
            "the airborne aircraft would reroute to hub-b and lose six minutes",
            "its reserve margin would fall to 42 Wh",
        ),
        expires_at_us=expires_at_us,
        created_at_us=MINUTE_US,
    )


def _solver_requests() -> Sequence[AssignmentRequest]:
    return tuple(
        AssignmentRequest(
            mission_id=f"m-{index}",
            aircraft_id=f"ac-{index % 3}",
            units=100 + index,
            earliest_start_us=index * MINUTE_US,
            latest_start_us=index * MINUTE_US + 20 * MINUTE_US,
            duration_us=5 * MINUTE_US,
            hub_facility_id="hub-a",
            energy_margin_wh=20 + index,
            tie_index=index,
        )
        for index in range(6)
    )


def _hashseed_script() -> str:
    """Print one digest, in a fresh interpreter, for a fixed dispatch round."""
    return "\n".join(
        (
            "from collections import OrderedDict",
            "from droneops_contracts import AircraftStatus, LinkQuality, PriorityClass",
            "from droneops_dispatcher import *",
            "from droneops_energy import snapshot_from_rows",
            "fleet = [FleetAircraft('ac-%d' % i,"
            " AircraftStatus.OFFLINE if i == 0 else (AircraftStatus.MAINTENANCE if i == 1"
            " else AircraftStatus.AVAILABLE),",
            " LinkQuality.LOST if i == 2 else LinkQuality.GOOD,",
            " 'airframe-x-sim', 'illustrative-1', 400, 10000 - 100 * i, 'hub-a', 15000, 1500,",
            " 0, 10000, i * 60000000) for i in range(6)]",
            "policy = DispatchPolicy(config_release_sha='sha256:' + 'c' * 64)",
            "queue = DispatchQueue(policy=policy)",
            "for i in range(5):",
            "    queue.enqueue(QueuedMission('m-%d' % i, 'o-%d' % i, PriorityClass.ROUTINE,"
            " i * 60000000, 3600000000 + (i % 3)))",
            "order = [m.mission_id for m in queue.order(1800000000)]",
            "query = candidate_query(fleet, policy=policy, as_of_us=0, payload_g=500)",
            "weather = snapshot_from_rows([(0, 0, 0, 0)], station='s',"
            " source='frozen:t', bundle_sha256='e' * 64, retrieved_at_us=0)",
            "screens = screen_fleet(MissionLegs('m-1', 500, 2000000, 0, 1000000), weather,"
            " query.candidates, clock_us=0)",
            "rows = rank_scores([score_candidate(mission_id='m-1', aircraft_id=s.aircraft_id,"
            " margin_wh=s.margin_wh, earliest_start_us=0, duration_us=300000000,"
            " deadline_us=1800000000, waits=FacilityWaits(), policy=policy)"
            " for s in screens.feasible])",
            "pairs = [(order[i % len(order)], r) for i, r in enumerate(rows)]",
            "solution = solve_assignment([AssignmentRequest(m, r.aircraft_id, r.units, 0,"
            " 1800000000, 300000000, 'hub-a', r.energy_margin_wh, i) for i, (m, r) in"
            " enumerate(pairs)], policy=policy, crew_capacity={'hub-a': 1}, round_id='r')",
            "keys = OrderedDict((row.aircraft_id, row.units) for row in rows)",
            "print(solution.digest() + '|' + '|'.join(order) + '|' + query.query_digest"
            " + '|' + '|'.join(keys))",
        )
    )
