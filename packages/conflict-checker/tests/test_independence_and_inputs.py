"""Independence is the product, so it gets tested as a property of the source.

``.importlinter`` enforces the contract, and ``tests/security/test_import_linter_bites.py``
proves the contract is wired. What that cannot see is the *soft* version of the same
failure: importing the planner's vocabulary by copy-paste, reusing ``geom-4d``'s policy
object under a different name, or reaching a verdict by calling something that already
calls the planner. These tests look at the text.
"""

from __future__ import annotations

import ast
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from checker_scenarios import EVALUATED_AT, POLICY_PATH, T0, context, point, record

from droneops_conflict_checker import (
    AllowRuleSet,
    PlanPoint,
    ReasonCode,
    allow_rules_from_yaml_text,
    check_pair,
    load_policy_file,
    policy_from_yaml_text,
)

SRC = Path(__file__).resolve().parents[1] / "src" / "droneops_conflict_checker"
MODULES = sorted(SRC.glob("*.py"))

#: The hard boundary ``.importlinter`` and ``scripts/check_boundaries.py`` enforce.
FORBIDDEN_ROOTS = {"droneops_planner", "droneops_geom_4d", "droneops_persistence"}

#: And the soft one: the checker must not even borrow the planner's *names*.
FORBIDDEN_SYMBOLS = ("RouteProfile", "ProtectedVolumePolicy", "SweptPrism", "pair_verdict",
                    "sweep_profile", "discretised_sweep", "profile_verdict", "VolumeIndex")


def imports_of(path: Path) -> set[str]:
    """Every module a source file imports, as written."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
    return found


@pytest.mark.unit
def test_no_module_imports_the_planner_geom_4d_or_the_write_model() -> None:
    assert MODULES, f"no checker sources under {SRC}"
    for module in MODULES:
        roots = {name.split(".")[0] for name in imports_of(module)}
        assert not roots & FORBIDDEN_ROOTS, f"{module.name} imports {sorted(roots & FORBIDDEN_ROOTS)}"


@pytest.mark.unit
def test_the_checker_does_not_borrow_the_planner_s_vocabulary() -> None:
    text = "\n".join(module.read_text(encoding="utf-8") for module in MODULES)
    for symbol in FORBIDDEN_SYMBOLS:
        assert symbol not in text, f"{symbol} is a geom-4d name; the checker must have its own"


@pytest.mark.unit
def test_shapely_is_confined_to_one_module() -> None:
    """The different geometry tool has to be *contained*, or the paths converge."""
    users = {module.name for module in MODULES
             if any(name.split(".")[0] == "shapely" for name in imports_of(module))}
    assert users == {"geometry.py"}, users


@pytest.mark.unit
def test_the_checker_has_its_own_sweep_and_its_own_policy_parser() -> None:
    sweep = (SRC / "sweep.py").read_text(encoding="utf-8")
    assert "def sweep_plan" in sweep and "PairFrame" in sweep
    policy = (SRC / "policy.py").read_text(encoding="utf-8")
    assert "def policy_from_mapping" in policy and "protected_volume" in policy
    assert "leg_pad_us()" in sweep, "the sweep must pad through the policy, not a literal"
    for module in MODULES:
        roots = {name.split(".")[0] for name in imports_of(module)}
        assert "droneops_geom_4d" not in roots, module.name


@pytest.mark.unit
def test_the_legacy_interval_floor_is_untouched_and_still_exported() -> None:
    """The stub's vocabulary and its interval function are what the reservations package uses."""
    from datetime import UTC

    from droneops_conflict_checker import check_interval_overlap, to_microseconds

    now = datetime(2026, 9, 30, tzinfo=UTC)
    later = datetime(2026, 9, 30, 1, tzinfo=UTC)
    assert to_microseconds(datetime(2026, 9, 30)) is None
    assert check_interval_overlap((now, later), (later, datetime(2026, 9, 30, 2, tzinfo=UTC))) == "SAFE"
    assert check_interval_overlap((now, datetime(2026, 9, 30, 2, tzinfo=UTC)), (later, datetime(2026, 9, 30, 2, tzinfo=UTC))) == "CONFLICT"
    assert check_interval_overlap((now, now), (now, later)) == "SAFE"


@pytest.mark.unit
def test_verdict_vocabulary_is_exactly_the_three_answers() -> None:
    from droneops_conflict_checker import VERDICTS

    assert VERDICTS == ("SAFE", "CONFLICT", "UNPROVEN")


# ------------------------------------------------------------------- policy file reading
@pytest.mark.unit
def test_policy_is_read_from_the_versioned_file_in_integer_units() -> None:
    from_file = load_policy_file(POLICY_PATH)
    from_text = policy_from_yaml_text(POLICY_PATH.read_text(encoding="utf-8"))
    assert from_file == from_text
    assert from_file.policy_version == "sim-policy-v1"
    assert from_file.horizontal_margin_mm == 15_000
    assert from_file.vertical_margin_mm == 10_000
    assert from_file.position_uncertainty_mm == 5_000
    assert from_file.timing_uncertainty_us == 5_000_000
    assert from_file.transition_buffer_us == 10_000_000
    assert from_file.authoritative is False
    assert from_file.key().count("|") == 9


@pytest.mark.unit
@pytest.mark.parametrize(
    "text",
    [
        "schema_version: 2\npolicy_version: x\naltitude_reference: AGL\nprotected_volume: {}\n",
        "policy_version: x\naltitude_reference: AGL\nprotected_volume: {}\n",
        "schema_version: 1\npolicy_version: ' '\naltitude_reference: AGL\nprotected_volume: {}\n",
        "schema_version: 1\npolicy_version: x\naltitude_reference: UNSPECIFIED\nprotected_volume: {}\n",
        "schema_version: 1\npolicy_version: x\naltitude_reference: BOGUS\nprotected_volume: {}\n",
        "schema_version: 1\npolicy_version: x\naltitude_reference: AGL\n",
        "schema_version: 1\npolicy_version: x\naltitude_reference: AGL\nprotected_volume: notamapping\n",
        "schema_version: 1\npolicy_version: x\naltitude_reference: AGL\nprotected_volume:\n  horizontal_margin_m: 'fifteen'\n",
        "schema_version: 1\npolicy_version: x\naltitude_reference: AGL\nprotected_volume:\n  horizontal_margin_m: 15\n",
        "not a mapping",
    ],
)
def test_a_policy_the_checker_cannot_understand_is_a_rejection(text: str) -> None:
    from droneops_conflict_checker import PolicyInputError

    with pytest.raises((PolicyInputError, ValueError)):
        policy_from_yaml_text(text)


@pytest.mark.unit
def test_negative_margins_are_rejected_rather_than_clamped() -> None:
    from droneops_conflict_checker import CheckerPolicy, PolicyInputError
    from droneops_geom_core import AltitudeReference

    with pytest.raises(PolicyInputError):
        CheckerPolicy(
            schema_version=1, policy_version="p", authoritative=False,
            altitude_reference=AltitudeReference.AGL, horizontal_margin_mm=-1,
            vertical_margin_mm=0, position_uncertainty_mm=0,
            timing_uncertainty_us=0, transition_buffer_us=0,
        )


@pytest.mark.unit
def test_the_checker_pads_every_leg_where_the_planner_pads_only_climbing_ones() -> None:
    """The conservatism is a number, so the number can be asserted.

    ``geom-4d`` adds the transition buffer to a leg only when the height changes; this
    checker adds it to every leg, and the difference is exactly the buffer. The test
    names the difference instead of trusting a comment about it.
    """
    active = load_policy_file(POLICY_PATH)
    assert active.leg_pad_us() == active.timing_uncertainty_us + active.transition_buffer_us
    assert active.crossing_tolerance_us() == 2 * active.leg_pad_us()
    assert active.lateral_reach_mm(0) > active.horizontal_margin_mm + active.position_uncertainty_mm
    assert active.vertical_reach_mm(0) >= active.vertical_margin_mm


@pytest.mark.unit
def test_allow_rules_are_named_versioned_and_refuse_to_be_vague() -> None:
    good = """
allow_rules:
  - rule_id: formation-a
    rule_version: v2
    policy_version: sim-policy-v1
    rationale: two aircraft of one formation briefed to fly in company
    approved_by: safety_reviewer
    max_contact_span_us: 5000000
    min_separation_mm: 0
    checks: [CONTINUOUS_SWEEP]
"""
    rules = allow_rules_from_yaml_text(good).rules
    assert len(rules) == 1
    assert rules[0].rule_version == "v2"
    assert rules[0].covers_checks(["CONTINUOUS_SWEEP"])
    assert not rules[0].covers_checks(["ADJACENT_CORRIDOR"])
    assert not rules[0].covers_checks([]), "an exception is opt-in per check"

    for broken in (
        "allow_rules: 3",
        "allow_rules:\n  - rule_id: x\n    rule_version: ' '\n",
        "allow_rules:\n  - rule_id: x\n    rule_version: v1\n    rationale: ' '\n"
        "    approved_by: safety_reviewer\n    max_contact_span_us: 1\n    min_separation_mm: 0\n",
        "allow_rules:\n  - rule_id: x\n    rule_version: v1\n    rationale: r\n"
        "    approved_by: a\n    max_contact_span_us: -1\n    min_separation_mm: 0\n",
    ):
        with pytest.raises(ValueError):
            allow_rules_from_yaml_text(broken)


# ----------------------------------------------------------------------- UNPROVEN triggers
def unprovable_pair(**overrides):
    """A pair that clears geometrically, so any UNPROVEN must come from the input."""
    ctx = context(**overrides.get("ctx", {}))
    left = record("PLAN-A", [point(0, 0), point(100, 0, at_s=10)], policy_active="sim-policy-v1",
                  **overrides.get("left", {}))
    right = record("PLAN-B", [point(0, 500), point(100, 500, at_s=10)], policy_active="sim-policy-v1",
                   **overrides.get("right", {}))
    return check_pair(left, right, ctx, rules=overrides.get("rules", AllowRuleSet.empty()))


@pytest.mark.unit
def test_missing_altitude_reference_is_unproven_not_safe() -> None:
    ctx = context()
    broken = record("PLAN-A", [point(0, 0, reference=None), point(100, 0, at_s=10, reference=None)])
    other = record("PLAN-B", [point(0, 500), point(100, 500, at_s=10)])
    verdict = check_pair(broken, other, ctx)
    assert verdict.verdict == "UNPROVEN"
    assert ReasonCode.MISSING_ALTITUDE_REFERENCE in {r.code for r in verdict.reasons}
    assert verdict.blocks_release
    control = unprovable_pair()
    assert control.verdict == "SAFE", "the control pair must clear, or the test proves nothing"


@pytest.mark.unit
def test_a_plan_that_mixes_datums_has_no_single_reference() -> None:
    ctx = context()
    mixed = record("PLAN-A", [point(0, 0, reference="AGL"), point(100, 0, at_s=10, reference="AMSL")])
    other = record("PLAN-B", [point(0, 500), point(100, 500, at_s=10)])
    verdict = check_pair(mixed, other, ctx)
    assert verdict.verdict == "UNPROVEN"
    assert ReasonCode.MISSING_ALTITUDE_REFERENCE in {r.code for r in verdict.reasons}


@pytest.mark.unit
def test_comparing_across_datums_is_unproven_and_never_converted_by_guessing() -> None:
    ctx = context()
    agl = record("PLAN-A", [point(0, 0, reference="AGL"), point(100, 0, at_s=10, reference="AGL")])
    amsl = record("PLAN-B", [point(0, 500, reference="AMSL"),
                             point(100, 500, at_s=10, reference="AMSL")])
    verdict = check_pair(agl, amsl, ctx)
    assert verdict.verdict == "UNPROVEN"
    assert ReasonCode.CROSS_DATUM_COMPARISON in {r.code for r in verdict.reasons}


@pytest.mark.unit
def test_absent_provenance_is_unproven() -> None:
    ctx = context()
    naked = replace(record("PLAN-A", [point(0, 0), point(100, 0, at_s=10)]),
                    planner_claim=None, resource_keys=(), plan_version="")
    verdict = check_pair(naked, record("PLAN-B", [point(0, 500), point(100, 500, at_s=10)]), ctx)
    assert verdict.verdict == "UNPROVEN"
    assert ReasonCode.ABSENT_PROVENANCE in {r.code for r in verdict.reasons}


@pytest.mark.unit
def test_expired_restriction_data_is_unproven() -> None:
    answer = unprovable_pair(
        left={"restriction_expires_at": EVALUATED_AT - timedelta(seconds=1)}
    )
    assert answer.verdict == "UNPROVEN"
    assert ReasonCode.EXPIRED_RESTRICTION_DATA in {r.code for r in answer.reasons}


@pytest.mark.unit
def test_stale_telemetry_retains_the_protection_rather_than_releasing_it() -> None:
    answer = unprovable_pair(left={"telemetry_last_fix": EVALUATED_AT - timedelta(seconds=90)})
    assert answer.verdict == "UNPROVEN"
    assert ReasonCode.STALE_TELEMETRY in {r.code for r in answer.reasons}
    assert any("invariant I3" in reason.code.human for reason in answer.reasons)


@pytest.mark.unit
def test_a_malformed_or_reversed_or_repeating_profile_is_unproven() -> None:
    ctx = context()
    other = record("PLAN-B", [point(0, 500), point(100, 500, at_s=10)])
    cases = {
        "empty": record("PLAN-A", []),
        "repeating": record("PLAN-A", [point(0, 0), point(100, 0, at_s=0)]),
        "reversed": record("PLAN-A", [point(100, 0, at_s=20), point(0, 0, at_s=10)]),
        "no body": record("PLAN-A", [point(0, 0), point(100, 0, at_s=10)], radius_mm=0),
        "not a number": record("PLAN-A", [
            PlanPoint(at=T0, lat_deg=float("nan"), lon_deg=76.7687, altitude_m=40.0,
                      altitude_reference="AGL"),
            point(100, 0, at_s=10)]),
    }
    for label, broken in cases.items():
        verdict = check_pair(broken, other, ctx)
        assert verdict.verdict == "UNPROVEN", label
        assert verdict.reasons, label
        assert verdict.is_well_formed, label


@pytest.mark.unit
def test_a_naive_timestamp_cannot_be_placed_on_the_absolute_timeline() -> None:

    ctx = context()
    naive = record("PLAN-A", [point(0, 0), point(100, 0, at_s=10)])
    assert naive.points[1].at.tzinfo is not None
    stripped = replace(naive, points=tuple(
        replace(item, at=datetime(2026, 10, 1, 9, 0, item.at.minute * 0 + int(index * 10),
                                  tzinfo=UTC if index else None)
                .replace(tzinfo=None if index == 0 else UTC))
        for index, item in enumerate(naive.points)
    ))
    verdict = check_pair(stripped, record("PLAN-B", [point(0, 500), point(100, 500, at_s=10)]), ctx)
    assert verdict.verdict == "UNPROVEN"
    assert ReasonCode.NAIVE_ABSOLUTE_TIME in {r.code for r in verdict.reasons}


@pytest.mark.unit
def test_a_plan_built_under_another_policy_release_is_unproven() -> None:
    ctx = context()
    stale_release = record("PLAN-A", [point(0, 0), point(100, 0, at_s=10)], release="sim-policy-v0")
    verdict = check_pair(stale_release, record("PLAN-B", [point(0, 500), point(100, 500, at_s=10)]), ctx)
    assert verdict.verdict == "UNPROVEN"
    assert ReasonCode.POLICY_RELEASE_MISMATCH in {r.code for r in verdict.reasons}


@pytest.mark.unit
def test_an_unparsable_resource_key_is_unproven_not_ignored() -> None:
    ctx = context()
    odd = record("PLAN-A", [point(0, 0), point(100, 0, at_s=10)], lanes=["bogus:SEG-01"])
    verdict = check_pair(odd, record("PLAN-B", [point(0, 500), point(100, 500, at_s=10)]), ctx)
    assert verdict.verdict == "UNPROVEN"
    assert ReasonCode.UNKNOWN_RESOURCE_KEY in {r.code for r in verdict.reasons}


@pytest.mark.unit
def test_two_plans_too_far_apart_to_share_a_frame_are_unproven() -> None:
    """The checker refuses to compare millimetres from different projections."""
    ctx = context()
    from droneops_geom_core import MAX_PLANAR_EXTENT_M

    far = record("PLAN-A", [point(MAX_PLANAR_EXTENT_M * 3, 0), point(MAX_PLANAR_EXTENT_M * 3 + 100, 0,
                   at_s=10)])
    verdict = check_pair(far, record("PLAN-B", [point(0, 0), point(100, 0, at_s=10)]), ctx)
    assert verdict.verdict == "UNPROVEN"
    assert re.search("projection|extent", " ".join(r.detail for r in verdict.reasons), re.I)


@pytest.mark.unit
def test_a_non_committed_record_is_not_silently_treated_as_safe() -> None:
    ctx = context()
    tentative = record("PLAN-A", [point(0, 0), point(100, 0, at_s=10)], status="tentative")
    verdict = check_pair(tentative, record("PLAN-B", [point(0, 0), point(100, 0, at_s=10)]), ctx)
    assert verdict.verdict == "UNPROVEN"
    assert ReasonCode.NOT_A_COMMITTED_RECORD in {r.code for r in verdict.reasons}


@pytest.mark.unit
def test_every_unproven_answer_carries_a_reason_and_a_code_that_explains_itself() -> None:
    for code in ReasonCode:
        assert code.human.strip(), code
        assert len(code.human) > 30, f"{code} explains nothing"
