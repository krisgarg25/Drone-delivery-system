"""The seven Phase 2 golden conflict scenarios, plus the frame-sample contrast.

``technical-build/05`` lists what has to be demonstrated before Phase 2 can close, and
``technical-build/06`` section 2 states the invariant those scenarios exist to exercise:
no two committed reservations overlap under the active policy unless an explicit allow
rule and the independent checker agree.

This suite reads the bundles as data. Nothing here builds a scenario in code: if the
JSON on disk and the checker's answer disagree, the build fails, which is the only way
the golden files stay honest as the checker changes.

Every bundle must carry ``what_this_does_not_prove``. That field is not decoration. A
green conflict scenario is exactly the kind of result an organisation over-reads, and
the suite refuses to run a bundle that cannot say what its own limits are.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from droneops_conflict_checker import (
    BundleError,
    ScenarioBundle,
    bundle_from_document,
    check_pair,
    load_bundle,
)

GOLDEN_DIR = Path(__file__).resolve().parent
BUNDLES = tuple(sorted(GOLDEN_DIR.glob("gc*.json")))

#: The seven scenarios ``technical-build/05`` Phase 2 names, by their own titles.
REQUIRED_SCENARIOS = {
    "same corridor opposite directions",
    "crossing",
    "climb through a layer",
    "same segment different times",
    "delayed telemetry",
    "airborne obstruction",
    "two simultaneous commits",
}

#: gc08 is the extra case the checker's whole design rests on, and gc09 the exception
#: mechanism the Phase 2 exit criterion requires.
EXTRA_SCENARIOS = {
    "fast aircraft between two frames",
    "both sides agree on one named exception",
}

CONFLICT_SCENARIOS = {
    "gc01", "gc02", "gc03", "gc06", "gc07", "gc08", "gc09",
}
SAFE_SCENARIOS = {"gc04"}
UNPROVEN_SCENARIOS = {"gc05"}


def normalised(bundle: ScenarioBundle) -> str:
    return " ".join(bundle.title.lower().replace(",", "").split())


def names(bundles: tuple[ScenarioBundle, ...]) -> set[str]:
    return {normalised(bundle) for bundle in bundles}


@pytest.mark.golden
def test_every_required_phase_two_scenario_is_present() -> None:
    assert BUNDLES, f"no golden bundles in {GOLDEN_DIR}"
    present = names(tuple(load_bundle(path) for path in BUNDLES))
    missing = REQUIRED_SCENARIOS - present
    assert not missing, f"Phase 2 golden scenarios missing: {sorted(missing)}"
    assert present >= EXTRA_SCENARIOS, f"contrast scenarios missing: {EXTRA_SCENARIOS - present}"


@pytest.mark.golden
def test_there_are_at_least_the_seven_named_conflict_scenarios() -> None:
    assert len(BUNDLES) >= 8, "seven scenarios plus the frame-sample contrast is the floor"


@pytest.mark.golden
@pytest.mark.parametrize("path", BUNDLES, ids=lambda path: path.stem)
def test_bundle_is_complete_and_self_describing(path: Path) -> None:
    document = json.loads(path.read_text(encoding="utf-8"))
    bundle = load_bundle(path)

    assert bundle.description.strip()
    assert bundle.invariant.startswith(("I1", "I3")), "a scenario must name its invariant"
    assert bundle.does_not_prove, f"{path.name}: `what_this_does_not_prove` is mandatory"
    for entry in bundle.does_not_prove:
        assert len(entry) > 40, f"{path.name}: a not proven field that short explains nothing"

    for key in ("scenario_id", "title", "description", "invariant",
                "what_this_does_not_prove", "policy", "evaluation", "records", "expected"):
        assert key in document, f"{path.name}: field {key!r} is required"
    for expectation in bundle.expectations:
        assert expectation.does_not_prove, f"{path.name}: an expectation without limits"
        assert expectation.verdict in ("SAFE", "CONFLICT", "UNPROVEN")
        assert expectation.invariants, f"{path.name}: no invariant claimed"


@pytest.mark.golden
@pytest.mark.parametrize("path", BUNDLES, ids=lambda path: path.stem)
def test_checker_reproduces_every_expected_verdict_and_metric(path: Path) -> None:
    bundle = load_bundle(path)
    for expectation in bundle.expectations:
        verdict, _ = bundle.evaluate(expectation)
        assert verdict.verdict == expectation.verdict, (
            f"{bundle.scenario_id}/{expectation.basis}: expected {expectation.verdict}, "
            f"got {verdict.verdict} ({verdict.explain()})"
        )
        assert verdict.is_well_formed, f"{bundle.scenario_id}: a malformed verdict {verdict.verdict}"
        for name in expectation.checks:
            assert name in {contact.check.value for contact in verdict.contacts}, (
                f"{bundle.scenario_id}: expected check {name} did not fire"
            )
        found = verdict.metrics.as_dict()
        for key, value in expectation.metrics.items():
            assert key in found, f"{bundle.scenario_id}: no such metric {key}"
            assert found[key] == pytest.approx(value, rel=1e-9, abs=1e-9), (
                f"{bundle.scenario_id}/{expectation.basis}: metric {key} moved from {value} "
                f"to {found[key]}"
            )


@pytest.mark.golden
@pytest.mark.parametrize("path", BUNDLES, ids=lambda path: path.stem)
def test_release_semantics_follow_the_invariant(path: Path) -> None:
    bundle = load_bundle(path)
    _, report = bundle.run()
    scenario = bundle.scenario_id

    if scenario in CONFLICT_SCENARIOS:
        assert report.verdict == "CONFLICT"
        assert any(item.verdict == "CONFLICT" for item in report.violations)
    elif scenario in SAFE_SCENARIOS:
        assert report.verdict == "SAFE"
        assert not report.blocks_release, report.release_blockers()
    elif scenario in UNPROVEN_SCENARIOS:
        assert report.verdict == "UNPROVEN"
        assert report.blocks_release, "an unprovable pair must never release airspace"
        assert not any(item.verdict == "SAFE" for item in report.violations)
    else:  # pragma: no cover - the suite is the registry
        pytest.fail(f"{scenario}: not classified, so its release rule is untested")

    assert report.certified is False, (
        "no bundle in this suite runs under an authoritative policy, so nothing here may "
        "be described as a certified clearance"
    )


@pytest.mark.golden
def test_conflicts_without_an_agreed_exception_are_release_blockers() -> None:
    for path in BUNDLES:
        bundle = load_bundle(path)
        if bundle.scenario_id in ("gc04", "gc09"):
            continue
        _, report = bundle.run()
        assert report.blocks_release, f"{bundle.scenario_id} must block release"
        assert report.release_blockers(), f"{bundle.scenario_id} must say why"


@pytest.mark.golden
def test_frame_sample_clears_the_pair_the_sweep_convicts() -> None:
    """The contrast that proves the checker sweeps instead of sampling.

    gc08 holds one pair twice: once asked continuously, once asked at 1 Hz. The
    sampled answer is SAFE and the swept answer is CONFLICT. If a future change
    makes those two agree, either the sweep got dumber or the frames got smarter,
    and in both cases the reason this suite exists has gone.
    """
    bundle = load_bundle(GOLDEN_DIR / "gc08-fast-aircraft-between-frames.json")
    swept = next(item for item in bundle.expectations if item.basis == "CONTINUOUS")
    sampled = next(item for item in bundle.expectations if item.basis == "FRAME")
    swept_verdict, _ = bundle.evaluate(swept)
    sampled_verdict, _ = bundle.evaluate(sampled)
    assert swept_verdict.verdict == "CONFLICT"
    assert sampled_verdict.verdict == "SAFE"
    assert sampled_verdict.metrics.contact_windows == 0
    assert swept_verdict.first_contact_us is not None


@pytest.mark.golden
def test_agreed_exception_reports_the_contact_and_releases_the_pair() -> None:
    """An exception does not hide the overlap; it excuses it, and only when both agree."""
    bundle = load_bundle(GOLDEN_DIR / "gc09-agreed-allow-rule-exception.json")
    verdict, report = bundle.run()
    assert verdict.verdict == "CONFLICT", "the contact is a fact and the checker does not un-say it"
    assert verdict.grant is not None
    assert verdict.grant.rule_id == "sim-formation-v1"
    assert verdict.grant.within_envelope
    assert not report.blocks_release, report.release_blockers()
    assert not report.disagreements, (
        "both sides named the same rule at the same version, so there is no disagreement"
    )


@pytest.mark.golden
def test_the_same_exception_with_a_narrower_envelope_blocks_again() -> None:
    """Same pair, same rule id, one number changed: the geometric envelope holds."""
    path = GOLDEN_DIR / "gc09-agreed-allow-rule-exception.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["allow_rules"][0]["max_contact_span_us"] = 1_000
    bundle = bundle_from_document(document, path.name)
    verdict, report = bundle.run()
    assert verdict.verdict == "CONFLICT"
    assert verdict.grant is not None and not verdict.grant.within_envelope
    assert report.blocks_release


@pytest.mark.golden
def test_an_exception_only_one_side_named_is_not_an_agreement() -> None:
    """Requirement: a policy exception must be named by both sides or it does not exist."""
    path = GOLDEN_DIR / "gc09-agreed-allow-rule-exception.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["records"][1]["planner_claim"]["allow_rule_id"] = None
    bundle = bundle_from_document(document, path.name)
    verdict, report = bundle.run()
    assert verdict.grant is None, "a rule cited by one plan is a wish, not an agreement"
    assert report.blocks_release


@pytest.mark.golden
def test_a_bundle_that_cannot_be_read_is_an_error_not_a_skip() -> None:
    with pytest.raises(BundleError):
        bundle_from_document({"scenario_id": "broken"}, "broken")
    for key in ("description", "invariant", "what_this_does_not_prove", "expected"):
        document = json.loads((GOLDEN_DIR / "gc04-same-segment-different-times.json").read_text(
            encoding="utf-8"))
        del document[key]
        with pytest.raises(BundleError):
            bundle_from_document(document, f"missing-{key}")


@pytest.mark.golden
def test_golden_pairs_are_symmetric_under_argument_order() -> None:
    """No scenario's answer may depend on which plan was handed over first."""
    for path in BUNDLES:
        bundle = load_bundle(path)
        for expectation in bundle.expectations:
            if expectation.pair is None:
                continue
            left = bundle.record(expectation.pair[0])
            right = bundle.record(expectation.pair[1])
            forward = check_pair(left, right, bundle.context, basis=expectation.basis,
                                 rules=bundle.rules)
            backward = check_pair(right, left, bundle.context, basis=expectation.basis,
                                 rules=bundle.rules)
            assert forward.verdict == backward.verdict, bundle.scenario_id
            assert forward.metrics.as_dict() == backward.metrics.as_dict(), bundle.scenario_id
