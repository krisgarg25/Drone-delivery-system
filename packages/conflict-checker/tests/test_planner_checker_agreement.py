"""The two implementations must agree on verdicts and disagree on methods.

``technical-build/06-testing-and-safety.md`` invariant I1 makes a planner-accepted,
checker-rejected pair a release blocker, and ``technical-build/05`` Phase 2 says the
same thing about the two implementations disagreeing. That is only a workable rule if
"disagree" means *different construction*, not *different reading of the policy*: a
validator that pads level flight with a manoeuvre buffer convicts plans the planner can
never produce, and every release blocks forever. So this file pins both halves.

What it does, concretely
    One scenario table. Each row is built twice, from the same numbers: once through
    ``droneops_geom_4d`` (policy parsed by its own reader, protected volumes as padded
    moving prisms, contact decided by a closed-form rational quadratic), and once through
    ``droneops_conflict_checker`` (policy parsed by *its* reader, protected volumes as
    motion plus a held head and tail, contact decided by the lateral distance of a
    relative track on shapely floats). Then every row's answer is compared.

    The sweep covers separation in time, lateral distance and vertical distance, and both
    level and altitude-changing legs, because the disagreement this suite exists to end
    was exactly a padding rule that depends on whether a leg changes height.

What it is not
    Not a claim that the checker and the planner give the same *whole* verdict. The
    checker runs two inspections the planner does not have - the time-blind
    adjacent/shared-lane test and the crossing-arrival test - and those are documented
    over-approximations the Phase 2 exit criterion answers with an allow rule. What is
    compared here is the protected-volume sweep, which is the one place the padding rule
    lives, so the checker's contacts are filtered to that question.
"""

from __future__ import annotations

import itertools
import math
from pathlib import Path

import pytest
from checker_scenarios import ORIGIN, context, place, point, record

from droneops_conflict_checker import (
    CheckName,
    PairFrame,
    check_pair,
    sweep_plan,
)
from droneops_conflict_checker import (
    PolicyInputError as CheckerRefusal,
)
from droneops_conflict_checker import (
    load_policy_file as load_checker_policy,
)
from droneops_conflict_checker import (
    policy_from_yaml_text as read_checker_policy_text,
)
from droneops_geom_4d import (
    PolicyInputError as PlannerRefusal,
)
from droneops_geom_4d import (
    ProfilePoint,
    ProtectedBody,
    RouteProfile,
    Verdict,
    conflict_verdict,
    sweep_profile,
)
from droneops_geom_4d import (
    load_policy_file as load_planner_policy,
)
from droneops_geom_4d import (
    policy_from_yaml_text as read_planner_policy_text,
)
from droneops_geom_core import AltitudeReference, LocalPlane, metres_to_millimetres

REPO_ROOT = Path(__file__).resolve().parents[3]
POLICY_PATH = REPO_ROOT / "contracts" / "configuration" / "protected_volume_policy.yaml"

#: The one projected frame the planner's profiles are built on. The checker builds its
#: own frame per pair, on purpose; both start from the same geodetic points.
PLANE = LocalPlane(ORIGIN)

#: The body both sides protect: 0.25 m radius, 0.15 m half height, described once as a
#: planner ``ProtectedBody`` and once as the millimetres a ledger row carries.
BODY_RADIUS_MM = 250
BODY_HALF_HEIGHT_MM = 150

#: A leg is (start altitude, end altitude) in metres AGL. ``alt_from != alt_to`` is the
#: policy's manoeuvre test, so a row with different altitudes climbs or descends and a
#: row with equal ones is straight and level.
CLIMB = (30.0, 50.0)
LEVEL_LOW = (40.0, 40.0)
LEVEL_HIGH = (65.0, 65.0)

#: Separations deliberately away from the contact threshold (2 x 20.25 m lateral,
#: 2 x 20.3 m vertical), so the comparison tests the padding rule and not the 1 mm
#: tolerance the checker adds to keep its own float arithmetic honest.
LATERAL_OFFSETS_M = (0.0, 30.0, 38.0, 45.0, 60.0)
VERTICAL_PROFILES = (
    (LEVEL_LOW, LEVEL_LOW),
    (LEVEL_LOW, LEVEL_HIGH),
    (CLIMB, CLIMB),
    (CLIMB, LEVEL_LOW),
)
TIME_GAPS_S = (0, 4, 8, 12, 16, 18, 22, 26, 34)
LEG_DURATION_S = 10
LEG_LENGTH_M = 100.0

#: The contacts that *are* the protected-volume sweep, i.e. the answer the padding rule
#: decides. CROSSING_VOLUME and ADJACENT_CORRIDOR are excluded: separate, deliberately
#: coarser inspections, answered by an allow rule rather than by a second padding rule.
SWEEP_CHECKS = frozenset({CheckName.CONTINUOUS_SWEEP, CheckName.TRANSITION_BUFFER})


def leg(
    *,
    east_from_m: float = 0.0,
    north_m: float = 0.0,
    alt: tuple[float, float] = LEVEL_LOW,
    start_s: int = 0,
    reverse: bool = False,
) -> dict:
    """One row of the scenario table, in the units both builders consume.

    ``reverse`` sends the leg back west along the same lane, which is how a head-on pair
    is written: same lane, same instants, opposite headings.
    """
    west = east_from_m - LEG_LENGTH_M if reverse else east_from_m + LEG_LENGTH_M
    return {
        "east_from_m": east_from_m,
        "east_to_m": west,
        "north_m": north_m,
        "alt_from_m": alt[0],
        "alt_to_m": alt[1],
        "start_s": start_s,
    }


def planner_profile(*, east_from_m: float, east_to_m: float, north_m: float, alt_from_m: float,
                    alt_to_m: float, start_s: int, version: str) -> RouteProfile:
    """The plan through ``droneops_geom_4d``: geodetic points in, swept prisms out."""
    speed_mmps = int(LEG_LENGTH_M * 1000 / LEG_DURATION_S)
    points = []
    for index, (east, altitude) in enumerate(
        ((east_from_m, alt_from_m), (east_to_m, alt_to_m))
    ):
        geo = place(east, north_m)
        points.append(
            ProfilePoint.at(
                (start_s + index * LEG_DURATION_S) * 1_000_000,
                geo.lat_deg,
                geo.lon_deg,
                metres_to_millimetres(altitude),
                AltitudeReference.AGL,
                speed_mmps,
            )
        )
    return RouteProfile.from_points(
        points,
        plan_version=version,
        source=f"agreement-{version}",
        authoritative=False,
        plane=PLANE,
    )


def record_points(spec: dict) -> list:
    """The same numbers as the planner is given, expressed as committed ledger points."""
    return [
        point(
            spec["east_to_m"] if index else spec["east_from_m"],
            spec["north_m"],
            altitude_m=spec["alt_to_m"] if index else spec["alt_from_m"],
            at_s=spec["start_s"] + index * LEG_DURATION_S,
        )
        for index in (0, 1)
    ]


def planner_says_conflict(left: dict, right: dict) -> bool:
    """The planner's answer, from its own policy object and its own geometry."""
    planner_policy = load_planner_policy(POLICY_PATH)
    body = ProtectedBody.of(BODY_RADIUS_MM / 1000.0, BODY_HALF_HEIGHT_MM / 1000.0, "agreement")
    verdict = conflict_verdict(
        planner_profile(**left, version="PLAN-A"),
        planner_profile(**right, version="PLAN-B"),
        body,
        planner_policy,
    )
    assert verdict.verdict is not Verdict.UNPROVEN, f"planner could not decide: {verdict.cause}"
    return verdict.verdict is Verdict.CONFLICT


def checker_sweep_says_conflict(left: dict, right: dict) -> bool:
    """The checker's answer, from its own policy parse and its own sweep."""
    pair = check_pair(
        record("PLAN-A", record_points(left), ["SEG-A"]),
        record("PLAN-B", record_points(right), ["SEG-B"]),
        context(),
        basis="CONTINUOUS",
    )
    assert pair.verdict != "UNPROVEN", f"checker could not decide: {pair.reasons}"
    return any(contact.check in SWEEP_CHECKS for contact in pair.contacts)


def cases() -> list[tuple[str, dict, dict]]:
    """Every combination of time gap, lateral offset, vertical profile and leg kind."""
    out: list[tuple[str, dict, dict]] = []
    for gap, north, (alt_left, alt_right) in itertools.product(
        TIME_GAPS_S, LATERAL_OFFSETS_M, VERTICAL_PROFILES
    ):
        label = f"gap{gap}s-north{north}m-left{alt_left}-right{alt_right}"
        out.append(
            (
                label,
                leg(north_m=north, alt=alt_left),
                leg(north_m=north, alt=alt_right, start_s=gap),
            )
        )
    # Head-on on one lane: same track, opposite directions, both must convict.
    out.append(("head-on-same-lane", leg(), leg(east_from_m=LEG_LENGTH_M, reverse=True)))
    # The same head-on pair written as a climb, so the manoeuvre pad is exercised too.
    out.append(("head-on-climbing", leg(alt=CLIMB), leg(east_from_m=LEG_LENGTH_M, alt=CLIMB,
                                                       reverse=True)))
    return out


@pytest.mark.parametrize(
    ("label", "left", "right"),
    cases(),
    ids=[str(case[0])[:60] for case in cases()],
)
def test_planner_and_checker_reach_the_same_verdict_on_the_same_pair(
    label: str, left: dict, right: dict
) -> None:
    """The resolution of the disagreement: one policy, one verdict per pair, two codes."""
    planner = planner_says_conflict(left, right)
    checker = checker_sweep_says_conflict(left, right)
    assert planner == checker, (
        f"{label}: planner conflict={planner} but checker sweep conflict={checker} - the "
        "two implementations read the same protected-volume policy and must not answer "
        "the same pair two different ways"
    )


def test_the_agreement_sweep_actually_covers_both_answers_and_both_leg_kinds() -> None:
    """A green agreement test that only ever said SAFE would prove nothing.

    The sweep must contain conflicts as well as clearances, and level legs as well as
    manoeuvre legs, or it cannot distinguish "the padding rule agrees" from "nothing was
    ever close enough to disagree".
    """
    verdicts = [planner_says_conflict(left, right) for _, left, right in cases()]
    assert any(verdicts), "the sweep found no conflict at all, so it tested nothing"
    assert not all(verdicts), "the sweep convicted everything, so it tested nothing"
    level = [pair for pair in VERTICAL_PROFILES if pair[0] == pair[1]]
    manoeuvre = [pair for pair in VERTICAL_PROFILES if pair[0] != pair[1]]
    assert level, "the grid holds no level pair, so the padding rule was never tested there"
    assert manoeuvre, "the grid holds no manoeuvre pair, so the buffer was never tested"
    rows = len(TIME_GAPS_S) * len(LATERAL_OFFSETS_M) * len(VERTICAL_PROFILES)
    assert len(verdicts) == rows + 2, "the head-on rows left the grid"


@pytest.mark.unit
def test_the_two_readers_of_the_policy_file_still_parse_it_two_different_ways() -> None:
    """Agreement is about the reading; the construction stays independent, visibly.

    The same five numbers out of two parsers, and a protected-volume set built one way
    per leg by the planner versus three pieces per leg by the checker - so neither answer
    can be the other's echo, and the verdict equality above is evidence rather than a
    tautology.
    """
    planner_policy = load_planner_policy(POLICY_PATH)
    checker_policy = load_checker_policy(POLICY_PATH)
    read = lambda item: (  # noqa: E731 - a local view of five fields, not a helper
        item.horizontal_margin_mm,
        item.vertical_margin_mm,
        item.position_uncertainty_mm,
        item.timing_uncertainty_us,
        item.transition_buffer_us,
    )
    assert read(planner_policy) == read(checker_policy)
    # The padding rule, stated once per side, lands on the same two numbers.
    assert planner_policy.leg_pad_us(False) == checker_policy.level_leg_pad_us()
    assert planner_policy.leg_pad_us(True) == checker_policy.manoeuvre_leg_pad_us()
    # And the provenance differs: the checker's key says where its margins came from.
    assert checker_policy.key().endswith("path=checker-own-parser")
    assert "path=checker-own-parser" not in planner_policy.key()

    body = ProtectedBody.of(BODY_RADIUS_MM / 1000.0, BODY_HALF_HEIGHT_MM / 1000.0, "agreement")
    climbing = leg(alt=CLIMB)
    prisms = sweep_profile(
        planner_profile(**climbing, version="PLAN-A"), body, planner_policy
    )
    assert len(prisms) == 1, "the planner builds one padded moving prism per leg"
    assert prisms[0].pad_us == planner_policy.leg_pad_us(True)

    planned = record("PLAN-A", record_points(climbing), ["SEG-A"])
    pieces = {piece.kind.value: piece for piece in
              sweep_plan(planned, PairFrame.of(planned), checker_policy).sorted_pieces()}
    assert set(pieces) == {"MOTION", "HOLD_BEFORE", "HOLD_AFTER"}, (
        "the checker builds motion plus a held head and tail"
    )
    pad = checker_policy.manoeuvre_leg_pad_us()
    assert pieces["HOLD_BEFORE"].start_us == pieces["MOTION"].start_us - pad
    assert pieces["HOLD_AFTER"].end_us == pieces["MOTION"].end_us + pad


@pytest.mark.unit
def test_a_policy_that_means_something_else_is_refused_by_both_sides() -> None:
    """A silently reinterpreted separation rule fails loudly, on both readers.

    The file's numbers are experiment parameters; its *meaning* is the contract. A policy
    that hands the transition buffer to level legs - or moves to an unsupported meaning
    version - is rejected rather than executed, by both readers, so no build can drift
    back into the disagreement this suite was written to end.
    """
    text = POLICY_PATH.read_text(encoding="utf-8")
    stolen = text.replace(
        "transition_buffer_applies_to: manoeuvre_leg_only",
        "transition_buffer_applies_to: every_leg",
    )
    assert stolen != text, "the policy file no longer states its meaning; the guard is moot"
    renumbered = text.replace("meaning_version: 2", "meaning_version: 3")
    assert renumbered != text, "the policy file no longer carries a meaning version"

    for document in (stolen, renumbered):
        with pytest.raises(PlannerRefusal):
            read_planner_policy_text(document)
        with pytest.raises(CheckerRefusal):
            read_checker_policy_text(document)


@pytest.mark.unit
def test_the_policy_file_states_its_own_rule_in_words() -> None:
    """The next reader must not have to infer this, which is what started the fight.

    The file carries the version and the sentence; both parsers turn it into the same
    numbers this suite then checks verdict by verdict.
    """
    text = POLICY_PATH.read_text(encoding="utf-8")
    assert "meaning_version: 2" in text
    assert "timing_uncertainty_applies_to: every_leg" in text
    assert "transition_buffer_applies_to: manoeuvre_leg_only" in text
    assert "manoeuvre_test: leg_altitude_changes" in text
    assert "EVERY leg, level or climbing" in text
    assert "ONLY a leg that contains a MANOEUVRE" in text

    planner_policy = load_planner_policy(POLICY_PATH)
    checker_policy = load_checker_policy(POLICY_PATH)
    for key in (planner_policy.key(), checker_policy.key()):
        assert "meaning=2:every_leg:manoeuvre_leg_only" in key, (
            "the meaning version must travel in the decision trace, not only in the file"
        )


@pytest.mark.unit
def test_contact_thresholds_stay_well_clear_of_the_checker_tolerance() -> None:
    """The grid sits metres away from the boundary the 1 mm tolerance could move.

    If a row sat within a millimetre of contact, the honest answer would be "the checker
    may convict a hair earlier", and this file would be reporting a policy disagreement
    that is only float padding. Checked, not assumed.
    """
    checker_policy = load_checker_policy(POLICY_PATH)
    lateral = 2 * checker_policy.lateral_reach_mm(BODY_RADIUS_MM)
    vertical = 2 * checker_policy.vertical_reach_mm(BODY_HALF_HEIGHT_MM)
    for offset in LATERAL_OFFSETS_M:
        assert abs(offset * 1000 - lateral) > 2_000, f"{offset} m sits on the contact threshold"
    steps = {abs(a[0] - b[0]) for a, b in VERTICAL_PROFILES} | {
        abs(a[1] - b[1]) for a, b in VERTICAL_PROFILES
    }
    for step in steps:
        assert abs(step * 1000 - vertical) > 2_000, f"{step} m sits on the vertical threshold"
    assert math.isclose(LEG_LENGTH_M / LEG_DURATION_S, 10.0)
