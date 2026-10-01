"""Behaviour: the four inspections, conservatism, batch mode, exceptions, disagreements.

These are the tests the Phase 2 exit criterion is written in: no two committed
reservations overlap under the active policy unless an explicit allow rule and the
independent checker agree - and where planner and checker disagree, the disagreement
blocks the release instead of being reconciled.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest
from checker_scenarios import EVALUATED_AT, context, point, policy, record, straight

from droneops_conflict_checker import (
    AllowRule,
    AllowRuleSet,
    CheckName,
    ReasonCode,
    absolute_us,
    check_pair,
    check_set,
    utc_instant,
)
from droneops_geom_core import GridSpec, Millimetre


def rules(*entries: AllowRule) -> AllowRuleSet:
    return AllowRuleSet(rules=tuple(sorted(entries, key=lambda r: (r.rule_id, r.rule_version))))


FORMATION = AllowRule(
    rule_id="formation-a", rule_version="v1", policy_version="sim-policy-v1",
    rationale="two aircraft of one formation briefed to fly the same lane in company",
    approved_by="safety_reviewer", max_contact_span_us=60_000_000, min_separation_mm=0,
    check_names=frozenset({CheckName.CONTINUOUS_SWEEP, CheckName.ADJACENT_CORRIDOR,
                           CheckName.CROSSING_VOLUME, CheckName.TRANSITION_BUFFER}),
)


@pytest.mark.unit
def test_head_on_pair_on_one_lane_is_a_conflict_found_by_the_sweep() -> None:
    ctx = context()
    left = record("PLAN-A", [point(-100, 0), point(0, 0, at_s=10), point(100, 0, at_s=20)])
    right = record("PLAN-B", [point(100, 0), point(0, 0, at_s=10), point(-100, 0, at_s=20)])
    answer = check_pair(left, right, ctx)
    assert answer.verdict == "CONFLICT"
    assert CheckName.CONTINUOUS_SWEEP in {contact.check for contact in answer.contacts}
    assert answer.first_contact_us is not None
    # the contact is reported where the two tracks actually met, at the lane midpoint
    assert abs(answer.contacts[0].east_mm) < 30_000  # within 30 m of the origin, in mm


@pytest.mark.unit
def test_sixty_metres_of_altitude_separates_a_pair_the_lanes_do_not() -> None:
    ctx = context()
    left = record("PLAN-A", [point(0, 0, altitude_m=10), point(100, 0, altitude_m=10, at_s=10)])
    right = record("PLAN-B", [point(0, 0, altitude_m=70), point(100, 0, altitude_m=70, at_s=10)])
    answer = check_pair(left, right, ctx)
    assert answer.verdict == "SAFE"
    assert answer.metrics.min_vertical_gap_mm > 20_000, "the vertical reach is 10.15 m each"


@pytest.mark.unit
def test_the_transition_buffer_pads_a_manoeuvre_leg_and_a_level_leg_gets_timing_only() -> None:
    """One padding rule, stated in the policy file, read the same way by both sides.

    A pair thirteen seconds apart on one lane. Level on both sides, each leg is padded
    with ``timing_uncertainty_s`` alone (5 s): the windows still overlap, but no instant
    puts the two bodies within reach of each other, so the *sweep* clears them - which is
    what the planner's own padded prisms say, and the two implementations must say the
    same thing about the same pair or invariant I1 blocks every release instead of a bad
    one. The pair is still convicted, by the time-blind shared-lane inspection, and that
    over-approximation is the documented one an allow rule exists to cover.

    Move the same pair into a climb and the transition buffer appears, because now there
    is a manoeuvre to be late: the identical geometry and the identical thirteen seconds
    become a contact the sweep itself finds.
    """
    active = policy()
    assert active.level_leg_pad_us() == active.timing_uncertainty_us
    assert active.manoeuvre_leg_pad_us() == (
        active.timing_uncertainty_us + active.transition_buffer_us
    )

    ctx = context()
    level_left = straight("PLAN-A", 0, 100, at_s=0)
    level_right = straight("PLAN-B", 0, 100, at_s=13)
    level = check_pair(level_left, level_right, ctx)
    level_checks = {contact.check for contact in level.contacts}
    assert CheckName.CONTINUOUS_SWEEP not in level_checks, (
        "13 s apart with 5 s of pad each side: the sweep sees no contact"
    )
    assert CheckName.TRANSITION_BUFFER not in level_checks, (
        "neither leg changes height, so neither leg carries a manoeuvre buffer"
    )
    assert CheckName.ADJACENT_CORRIDOR in level_checks, (
        "one lane, overlapping padded windows: the lane inspection still says no"
    )

    climb_left = record(
        "PLAN-A", [point(0, 0, altitude_m=30), point(100, 0, altitude_m=50, at_s=10)]
    )
    climb_right = record(
        "PLAN-B", [point(0, 0, altitude_m=30, at_s=13), point(100, 0, altitude_m=50, at_s=23)]
    )
    climbing = check_pair(climb_left, climb_right, ctx)
    sweep_checks = {contact.check for contact in climbing.contacts} & {
        CheckName.CONTINUOUS_SWEEP,
        CheckName.TRANSITION_BUFFER,
    }
    assert sweep_checks, (
        "the same thirteen seconds, now with a manoeuvre each side: 15 s of pad meets, "
        "and the contact is the sweep's own, not the lane inspection's"
    )
    assert climbing.verdict == "CONFLICT"


@pytest.mark.unit
def test_adjacent_corridors_are_inspected_from_the_committed_lane_map() -> None:
    """The corridor layer convicts a pair the sweep clears, and that is the design.

    Two parallel lanes 20 m apart - inside each other's 20.25 m protection, so the
    coarse lane test fires - with the second plan eighteen seconds behind the first,
    close enough that the two legs' timing-uncertainty windows still meet. The sweep
    follows each body along its own track and they are hundreds of metres apart whenever
    both are airborne, so it finds nothing. The lane test does
    not care where along the lane either aircraft was, and says these two lanes were
    never meant to be flown together. That gap is the over-approximation, and an
    allow rule is the only way through it.
    """
    ctx = context(adjacency={"SEG-A": ("SEG-B",), "SEG-B": ("SEG-A")})
    left = record("PLAN-A", [point(0, 0), point(100, 0, at_s=10)], ["SEG-A"])
    right = record("PLAN-B", [point(0, 20, at_s=18), point(100, 20, at_s=28)], ["SEG-B"])
    answer = check_pair(left, right, ctx)
    assert answer.verdict == "CONFLICT"
    assert CheckName.ADJACENT_CORRIDOR in {contact.check for contact in answer.contacts}
    assert {finding.relation for finding in answer.corridors} == {"ADJACENT"}
    assert answer.metrics.contact_windows >= 1


@pytest.mark.unit
def test_lanes_that_are_not_related_are_not_reported_as_adjacent() -> None:
    """The same geometry, with the adjacency line removed, clears.

    Without a declared relation the corridor test has nothing to say, and the only
    question left is the one the sweep answers: were they ever in the same place at
    the same instant. They were not.
    """
    ctx = context(adjacency={"SEG-A": ("SEG-B",)})
    left = record("PLAN-A", [point(0, 0), point(100, 0, at_s=10)], ["SEG-A"])
    right = record("PLAN-B", [point(0, 20, at_s=25), point(100, 20, at_s=35)], ["SEG-C"])
    answer = check_pair(left, right, ctx)
    assert answer.corridors == ()
    assert answer.verdict == "SAFE", "unrelated lanes, no shared instant: nothing to convict"


@pytest.mark.unit
def test_a_crossing_is_convicted_by_arrival_time_and_cleared_by_altitude() -> None:
    ctx = context()
    east = record("PLAN-A", [point(-100, 0), point(100, 0, at_s=10)], ["SEG-10"])
    north = record("PLAN-B", [point(0, -100), point(0, 100, at_s=10)], ["SEG-20"])
    together = check_pair(east, north, ctx)
    assert together.verdict == "CONFLICT"
    assert CheckName.CROSSING_VOLUME in {contact.check for contact in together.contacts}
    assert len(together.crossings) == 1
    assert together.crossings[0].arrival_gap_us == 0

    above = replace(north, points=(point(0, -100, altitude_m=90), point(0, 100, altitude_m=90, at_s=10)))
    apart = check_pair(east, above, ctx)
    assert apart.verdict == "SAFE", "the crossing is real but the heights are not contested"
    assert len(apart.crossings) == 1, "the finding is still reported; only the verdict changed"


@pytest.mark.unit
def test_a_fast_pair_between_two_frames_is_safe_to_a_sampler_and_conflicts_to_a_sweep() -> None:
    ctx = context()
    left = record("PLAN-A", [point(-60, 0), point(60, 0, at_s=1)], ["SEG-80"])
    right = record("PLAN-B", [point(60, 0), point(-60, 0, at_s=1)], ["SEG-80"])
    swept = check_pair(left, right, ctx, basis="CONTINUOUS")
    sampled = check_pair(left, right, ctx, basis="FRAME", step_us=1_000_000)
    assert swept.verdict == "CONFLICT" and sampled.verdict == "SAFE"
    assert sampled.metrics.contact_windows == 0
    assert swept.first_contact_us is not None
    mid = absolute_us(EVALUATED_AT) - absolute_us(EVALUATED_AT)
    assert mid == 0 and utc_instant(absolute_us(left.points[0].at)) == left.points[0].at


@pytest.mark.unit
def test_an_inflated_discretisation_never_clears_a_pair_the_sweep_convicted() -> None:
    ctx = context()
    left = record("PLAN-A", [point(-60, 0), point(60, 0, at_s=1)], ["SEG-80"])
    right = record("PLAN-B", [point(60, 0), point(-60, 0, at_s=1)], ["SEG-80"])
    for step in (1_000_000, 100_000, 10_000):
        answer = check_pair(left, right, ctx, basis="DISCRETISED", step_us=step)
        assert answer.verdict == "CONFLICT", f"step {step} walked back a proven contact"


@pytest.mark.unit
def test_a_batch_returns_every_violation_in_the_committed_set() -> None:
    ctx = context()
    same_lane = [straight(f"PLAN-{index}", 0, 100, at_s=index * 3) for index in range(4)]
    report = check_set(same_lane, ctx)
    assert report.records_checked == 4
    assert report.pairs_checked == 6
    assert report.verdict == "CONFLICT"
    assert len(report.violations) == 6, "every pair of one occupied lane is a violation"
    assert report.blocks_release
    assert len(report.release_blockers()) == 12, "six conflicts plus six disagreements"
    assert report.summary().startswith("CONFLICT over 4 record(s) and 6 pair(s)")


@pytest.mark.unit
def test_a_batch_ignores_records_that_hold_no_airspace_but_still_reports_their_defects() -> None:
    ctx = context()
    cancelled = straight("PLAN-Z", 0, 100, status="cancelled")
    report = check_set([straight("PLAN-A", 0, 100), cancelled], ctx)
    assert report.records_checked == 1, "a cancelled commitment holds no space"
    assert report.pairs_checked == 0
    tentative = straight("PLAN-T", 0, 100, status="tentative")
    out_of_scope = check_set([straight("PLAN-A", 0, 100), tentative], ctx)
    assert out_of_scope.records_checked == 1, "a tentative plan is not a commitment to check"
    assert not out_of_scope.blocks_release
    # ... but a *committed* record the checker cannot evaluate is never dropped.
    broken = record("PLAN-K", [])
    flagged = check_set([broken], ctx)
    assert flagged.defects, "an evaluable-but-broken commitment must appear as a defect"
    assert flagged.blocks_release


@pytest.mark.unit
def test_planner_safe_checker_conflict_is_always_a_release_blocker_and_never_reconciled() -> None:
    """The requirement in ``conventions.md`` section 6, tested as a property of the report."""
    ctx = context()
    left = straight("PLAN-A", 0, 100, claim="SAFE")
    right = straight("PLAN-B", 0, 100, at_s=2, claim="SAFE")
    report = check_set([left, right], ctx)
    assert report.verdict == "CONFLICT"
    kinds = {item.kind for item in report.disagreements}
    assert "PLANNER_SAFE_CHECKER_CONFLICT" in kinds
    assert all(item.blocks_release for item in report.disagreements)
    assert report.blocks_release
    # and nothing in the report has converted the disagreement back into a clearance
    assert not any(item.verdict == "SAFE" for item in report.violations)
    assert all(item.blocked for item in report.violations)


@pytest.mark.unit
def test_a_checker_that_went_softer_than_the_planner_is_also_a_blocker() -> None:
    ctx = context()
    left = straight("PLAN-A", 0, 100, claim="SAFE")
    right = straight("PLAN-B", 0, 100, at_s=600, claim="CONFLICT")
    report = check_set([left, right], ctx)
    assert report.verdict == "SAFE"
    assert {item.kind for item in report.disagreements} == {"PLANNER_CONFLICT_CHECKER_SAFE"}
    assert report.blocks_release, "a validator that is softer than its subject is broken"


@pytest.mark.unit
def test_an_unprovable_pair_is_never_excused_by_an_allow_rule() -> None:
    ctx = context()
    stale = straight("PLAN-A", 0, 100, telemetry_last_fix=EVALUATED_AT - timedelta(seconds=90))
    other = straight("PLAN-B", 0, 100, at_s=600)
    answer = check_pair(stale, other, ctx, rules=rules(FORMATION))
    assert answer.verdict == "UNPROVEN"
    assert answer.grant is None, "an exception excuses a contact, not an unknown"
    report = check_set([stale, other], ctx, rules=rules(FORMATION))
    assert report.blocks_release


@pytest.mark.unit
def test_an_exception_needs_both_sides_and_a_meeting_envelope() -> None:
    ctx = context()
    left = straight("PLAN-A", 0, 100, rule_id="formation-a", rule_version="v1")
    right = straight("PLAN-B", 0, 100, at_s=2, rule_id="formation-a", rule_version="v1")
    agreed = check_pair(left, right, ctx, rules=rules(FORMATION))
    assert agreed.grant is not None and agreed.grant.within_envelope
    assert agreed.verdict == "CONFLICT", "the contact stays on the record"
    report = check_set([left, right], ctx, rules=rules(FORMATION))
    assert not report.blocks_release
    assert not report.disagreements, "both sides named the same rule, so they agree"

    one_sided = check_pair(left, replace(right, planner_claim=replace(
        right.planner_claim, allow_rule_id=None, allow_rule_version=None)), ctx,
        rules=rules(FORMATION))
    assert one_sided.grant is None

    wrong_version = check_pair(left, replace(right, planner_claim=replace(
        right.planner_claim, allow_rule_version="v2")), ctx, rules=rules(FORMATION))
    assert wrong_version.grant is None

    withdrawn = check_pair(left, right, ctx, rules=rules(replace(FORMATION, active=False)))
    assert withdrawn.grant is None
    assert check_set([left, right], ctx, rules=rules(replace(FORMATION, active=False))).blocks_release

    too_narrow = check_pair(left, right, ctx, rules=rules(replace(
        FORMATION, max_contact_span_us=1)))
    assert too_narrow.grant is not None and not too_narrow.grant.within_envelope
    assert check_set([left, right], ctx, rules=rules(replace(FORMATION, max_contact_span_us=1))).blocks_release


@pytest.mark.unit
def test_a_rule_from_a_different_policy_release_does_not_transfer() -> None:
    ctx = context()
    left = straight("PLAN-A", 0, 100, rule_id="formation-a", rule_version="v1")
    right = straight("PLAN-B", 0, 100, at_s=2, rule_id="formation-a", rule_version="v1")
    answer = check_pair(left, right, ctx, rules=rules(replace(FORMATION, policy_version="other-release")))
    assert answer.grant is None, "exceptions are scoped to the policy release that wrote them"


@pytest.mark.unit
def test_the_contested_volume_cell_is_named_in_the_ledger_s_own_vocabulary() -> None:
    ctx = context(grid=GridSpec(epsg=32643, cell_size_mm=Millimetre(100_000),
                               vertical_band_mm=Millimetre(10_000)))
    left = straight("PLAN-A", 0, 100)
    right = straight("PLAN-B", 0, 100, at_s=2)
    report = check_set([left, right], ctx)
    assert report.violations[0].contested_cell, "a conflict must name the volume it is inside"
    assert "-" in report.violations[0].contested_cell or report.violations[0].contested_cell


@pytest.mark.unit
def test_a_pair_is_measured_on_one_frame_chosen_from_both_records() -> None:
    """The frame comes from the pair, so it cannot depend on which argument came first."""
    ctx = context()
    left = record("PLAN-A", [point(0, 0), point(100, 0, at_s=10)], ["SEG-A"])
    right = record("PLAN-B", [point(0, 50), point(100, 50, at_s=10)], ["SEG-B"])
    forward = check_pair(left, right, ctx)
    backward = check_pair(right, left, ctx)
    assert forward.subject == backward.subject == "PLAN-A|PLAN-B"
    assert forward.metrics.as_dict() == backward.metrics.as_dict()
    assert forward.verdict == backward.verdict


@pytest.mark.unit
def test_a_stationary_record_is_swept_by_the_same_path_as_a_moving_one() -> None:
    ctx = context()
    hover = record("PLAN-H", [point(0, 0), point(0, 0, at_s=60)], ["SEG-90"])
    passing = record("PLAN-B", [point(-100, 0, altitude_m=45), point(100, 0, altitude_m=45, at_s=10)])
    answer = check_pair(hover, passing, ctx)
    assert answer.verdict == "CONFLICT"
    assert answer.metrics.contact_windows >= 1


@pytest.mark.unit
def test_explain_and_summary_always_say_which_of_the_three_answers_it_was() -> None:
    ctx = context()
    for verdict, pair in {
        "SAFE": (straight("PLAN-A", 0, 100), straight("PLAN-B", 0, 100, at_s=600)),
        "CONFLICT": (straight("PLAN-A", 0, 100), straight("PLAN-B", 0, 100, at_s=2)),
    }.items():
        answer = check_pair(*pair, ctx)
        assert answer.verdict == verdict
        assert answer.explain().startswith("PLAN-A|PLAN-B")
        assert verdict in answer.explain()
    broken = check_pair(record("PLAN-A", []), straight("PLAN-B", 0, 100), ctx)
    assert broken.explain().startswith("PLAN-A|PLAN-B: UNPROVEN")


@pytest.mark.unit
def test_a_plan_point_missing_a_reference_never_reaches_the_geometry_layer() -> None:
    ctx = context()
    broken = record("PLAN-A", [point(0, 0, reference=None), point(100, 0, at_s=10)])
    answer = check_pair(broken, straight("PLAN-B", 0, 600), ctx)
    assert answer.metrics.piece_pairs_tested == 0, "no geometry without a datum"
    assert ReasonCode.MISSING_ALTITUDE_REFERENCE in {r.code for r in answer.reasons}


@pytest.mark.unit
def test_a_report_under_a_non_authoritative_policy_is_never_certified() -> None:
    ctx = context()
    clean = check_set([straight("PLAN-A", 0, 100), straight("PLAN-B", 0, 100, at_s=600)], ctx)
    assert clean.verdict == "SAFE"
    assert not clean.certified, "the simulation policy declares itself unvalidated, so no all-clear is proof"
