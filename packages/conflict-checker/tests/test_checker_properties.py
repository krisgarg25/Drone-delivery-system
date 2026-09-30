"""Property tests: the three answers have to hold for shapes nobody wrote a test for.

``technical-build/06`` section 3 asks for a property test over route pairs, and the
three properties here are the ones whose failure would mean the checker is not what
it claims to be:

1. **symmetry** - the verdict is a fact about two plans, not about the order two
   arguments were passed in;
2. **refinement monotonicity** - a discretised ``SAFE`` stays ``SAFE`` under a finer
   grid, because the inflated samples over-approximate the sweep rather than
   approximating it, and that is the only reason a sampled answer is worth anything;
3. **``UNPROVEN`` is never bare** - every unprovable answer carries a reason.

Plus the invariant itself, as a property: for any pair whose committed records both
say SAFE, a checker ``CONFLICT`` appears in the report as a release blocker and is
never reconciled into a clearance.

These generate *positions*, not scenarios: a plan is two to four points at grid
spacings on one projected frame, with a mixture of clean and deliberately broken
provenance so the unprovable branch is exercised too.
"""

from __future__ import annotations

from datetime import timedelta

from checker_scenarios import EVALUATED_AT, context, point, record
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from droneops_conflict_checker import (
    AllowRule,
    AllowRuleSet,
    CheckName,
    check_pair,
    check_set,
)

LANES = st.sampled_from(["SEG-A", "SEG-B", "SEG-C"])
OFFSET_M = st.integers(min_value=-400, max_value=400)
ALTITUDE_M = st.sampled_from([10.0, 25.0, 40.0, 55.0, 90.0])
STEP_SECONDS = st.sampled_from([1.0, 2.0, 5.0])

#: States, claims and datums, including the broken ones, so no branch is unreachable.
STATUS = st.sampled_from(["committed", "committed", "committed", "activated"])
CLAIM = st.sampled_from(["SAFE", "SAFE", "CONFLICT", "UNPROVEN"])
REFERENCE = st.sampled_from(["AGL", "AGL", "AGL", None, "AMSL", "UNSPECIFIED"])


@st.composite
def plan(draw: st.DrawsMethod, label: str) -> st.SearchStrategy:
    """A committed record of arbitrary shape, with provenance that may be incomplete."""
    count = draw(st.integers(min_value=1, max_value=4))
    east = draw(OFFSET_M)
    north = draw(OFFSET_M)
    altitude = draw(ALTITUDE_M)
    reference = draw(REFERENCE)
    start = draw(st.integers(min_value=0, max_value=240))
    points = [
        point(
            east + draw(st.integers(min_value=0, max_value=200)),
            north + draw(st.integers(min_value=0, max_value=200)),
            altitude_m=altitude + draw(st.integers(min_value=-20, max_value=20)),
            at_s=start + index * draw(st.integers(min_value=1, max_value=30)),
            reference=reference,
        )
        for index in range(count)
    ]
    return record(
        label,
        points,
        lanes=draw(st.lists(LANES, min_size=0, max_size=2)),
        status=draw(STATUS),
        claim=draw(CLAIM),
        telemetry_last_fix=EVALUATED_AT - timedelta(seconds=draw(st.integers(min_value=0,
                                                                            max_value=120))),
        restriction_expires_at=EVALUATED_AT + timedelta(seconds=draw(st.integers(-60, 600))),
    )


@settings(max_examples=120, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(left=plan("PLAN-A"), right=plan("PLAN-B"))
def test_the_verdict_is_symmetric_in_its_two_arguments(left, right) -> None:
    ctx = context()
    forward = check_pair(left, right, ctx)
    backward = check_pair(right, left, ctx)
    assert forward.subject == backward.subject
    assert forward.verdict == backward.verdict
    assert forward.metrics.as_dict() == backward.metrics.as_dict()
    assert forward.reason_text() == backward.reason_text()
    assert sorted(c.sort_key() for c in forward.contacts) == sorted(
        c.sort_key() for c in backward.contacts
    )


@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(left=plan("PLAN-A"), right=plan("PLAN-B"), step=STEP_SECONDS)
def test_a_discretised_safe_never_contradicts_a_finer_discretisation(left, right, step) -> None:
    """Coarse ``SAFE`` implies fine ``SAFE``, for every pair and every step.

    The inflated samples are an over-approximation of the sweep, so a coarse grid
    claims *at least* everything a finer one claims; a pair the coarse grid cleared
    cannot be a pair the finer grid convicts. The reverse is allowed and expected -
    a fine grid shrinks the inflation and may legitimately clear what the coarse one
    flagged - which is why this is one direction and not an equality.
    """
    ctx = context()
    step_us = int(step * 1_000_000)
    coarse = check_pair(left, right, ctx, basis="DISCRETISED", step_us=step_us)
    fine = check_pair(left, right, ctx, basis="DISCRETISED", step_us=step_us // 2)
    if coarse.verdict == "SAFE":
        assert fine.verdict == "SAFE", (
            f"a finer grid convicted a pair the coarse grid cleared: the inflation no "
            f"longer contains the sweep at step {step}s"
        )


@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(left=plan("PLAN-A"), right=plan("PLAN-B"), step=STEP_SECONDS)
def test_a_discretised_safe_also_clears_the_sweep_it_over_approximates(left, right, step) -> None:
    """Containment, stated the other way: no continuous contact survives a proven SAFE.

    If the discretised path says the inflated volumes never touch, the sweep they
    contain cannot touch either. A failure here means the sampled path stopped being
    an over-approximation and became a different question.
    """
    ctx = context()
    discretised = check_pair(left, right, ctx, basis="DISCRETISED",
                             step_us=int(step * 1_000_000))
    swept = check_pair(left, right, ctx, basis="CONTINUOUS")
    if discretised.verdict == "SAFE":
        assert swept.verdict != "CONFLICT", (
            "the sweep found a contact inside a volume the discretised path proved clear"
        )


@settings(max_examples=120, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(left=plan("PLAN-A"), right=plan("PLAN-B"))
def test_unproven_is_always_accompanied_by_a_reason(left, right) -> None:
    ctx = context()
    for basis, step in (("CONTINUOUS", None), ("DISCRETISED", 1_000_000), ("FRAME", 1_000_000)):
        answer = check_pair(left, right, ctx, basis=basis, step_us=step)
        assert answer.verdict in ("SAFE", "CONFLICT", "UNPROVEN")
        assert answer.is_well_formed, f"{answer.verdict} with no explanation: {answer.explain()}"
        if answer.verdict == "UNPROVEN":
            assert answer.reasons
            assert all(reason.detail.strip() for reason in answer.reasons)
            assert all(reason.code.human.strip() for reason in answer.reasons)
            assert answer.blocks_release
        if answer.verdict == "CONFLICT":
            assert answer.contacts
            assert answer.first_contact_us is not None


@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(left=plan("PLAN-A"), right=plan("PLAN-B"))
def test_a_planner_safe_checker_conflict_pair_is_a_release_blocker(left, right) -> None:
    """I1 as a property: nothing reconciles a checker conflict with a planner all-clear.

    Both committed records claim SAFE. If the checker disagrees, the report must block,
    must record the disagreement, and must not have converted either answer into a
    clearance - and the only way out is an allow rule *both* sides name.
    """
    from dataclasses import replace

    clean_left = replace(left, planner_claim=replace(left.planner_claim, verdict="SAFE",
                                                    allow_rule_id=None, allow_rule_version=None))
    clean_right = replace(right, planner_claim=replace(right.planner_claim, verdict="SAFE"),
                          status="committed")
    ctx = context()
    answer = check_pair(clean_left, clean_right, ctx)
    report = check_set([clean_left, clean_right], ctx)
    if answer.verdict == "CONFLICT":
        kinds = {item.kind for item in report.disagreements}
        assert "PLANNER_SAFE_CHECKER_CONFLICT" in kinds or answer.grant is not None
        assert report.blocks_release or (
            answer.grant is not None and answer.grant.within_envelope
        ), "a conflict with no agreed exception must block"
        assert all(item.verdict in ("CONFLICT", "UNPROVEN") for item in report.violations)
    if answer.verdict == "UNPROVEN":
        assert report.blocks_release, "an unprovable pair never releases airspace"


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(left=plan("PLAN-A"), right=plan("PLAN-B"))
def test_an_allow_rule_cannot_be_widened_by_the_pair_it_excuses(left, right) -> None:
    """A grant needs both sides to name it, and can never be wider than its own floor."""
    from dataclasses import replace

    ctx = context()
    answer = check_pair(left, right, ctx)
    if answer.verdict != "CONFLICT":
        return
    broad = AllowRuleSet(rules=(AllowRule(
        rule_id="everything", rule_version="v1", policy_version=ctx.policy.policy_version,
        rationale="a rule wide enough to test that the floor still binds",
        approved_by="safety_reviewer",
        max_contact_span_us=10**15,
        min_separation_mm=10**12,
        check_names=frozenset(item.value for item in CheckName),
    ),))
    # A rule that exists is not a rule that applies: neither record named it, so nothing
    # is excepted and the contact stands on its own.
    unclaimed = check_pair(left, right, ctx, rules=broad)
    assert unclaimed.grant is None
    assert check_set([left, right], ctx, rules=broad).blocks_release

    named_left = replace(left, planner_claim=replace(
        left.planner_claim, verdict="CONFLICT", allow_rule_id="everything",
        allow_rule_version="v1"))
    named_right = replace(right, planner_claim=replace(
        right.planner_claim, verdict="CONFLICT", allow_rule_id="everything",
        allow_rule_version="v1"))
    granted = check_pair(named_left, named_right, ctx, rules=broad)
    assert granted.grant is not None, "both sides named it, so the grant is offered"
    assert not granted.grant.within_envelope, (
        "a floor of a thousand kilometres is never met, so no exception may be granted"
    )
    assert check_set([named_left, named_right], ctx, rules=broad).blocks_release
