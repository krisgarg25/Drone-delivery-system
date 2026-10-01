"""Which records the checker agrees to argue about, and which it refuses to see.

``technical-build/11`` section 7 gives the checker the last word about a *set*, so the
set's membership is itself a safety decision. These tests pin the four admission rules of
:mod:`droneops_conflict_checker.candidates` directly, without a database in the way, so a
refusal in the ledger proofs can be attributed to a rule rather than to a rumour.

Admission is not conviction: a pair admitted here is only answered about by ``check_pair``.
What these tests do **not** prove is the metric of any verdict - they prove which questions
get asked, which is the step that a set built by resource key used to skip entirely.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from droneops_conflict_checker import (
    CommittedPlanRecord,
    EvaluationContext,
    PlanPoint,
    PlannerClaim,
    cells_adjacent,
    lane_keys,
    load_policy_file,
    select_adjacent_candidates,
    volumes_reach,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
POLICY = load_policy_file(REPO_ROOT / "contracts" / "configuration" / "protected_volume_policy.yaml")
T0 = datetime(2027, 3, 4, 6, 0, tzinfo=UTC)

#: C-01 and C-02 are one lane apart: 0.0002 degrees of longitude here is about 19 m, inside
#: the two plans' combined protected reach. C-03 is 0.04 degrees away - several hundred
#: metres, outside any reach - so it is the pair that must *not* be admitted.
NEAR_LON = 76.7802
FAR_LON = 76.8200


def context(adjacency: dict[str, tuple[str, ...]]) -> EvaluationContext:
    return EvaluationContext(policy=POLICY, evaluated_at=T0, adjacency=adjacency)


def plan(
    name: str,
    *,
    lane: str | None = None,
    cell: str | None = None,
    lon: float = 76.7800,
    minutes: int = 0,
    mission: str | None = None,
) -> CommittedPlanRecord:
    """A committed record on one lane (or one cell), flown from ``T0 + minutes``."""
    start = T0 + timedelta(minutes=minutes)
    key = f"corridor:{lane}" if lane is not None else f"volume_cell:{cell}"
    return CommittedPlanRecord(
        plan_version=name,
        mission_id=mission or f"mission-{name}",
        status="committed",
        points=(
            PlanPoint(start, 30.7500, lon, 60.0, "AGL", 12_000),
            PlanPoint(start + timedelta(minutes=5), 30.7700, lon + 0.02, 60.0, "AGL", 12_000),
        ),
        resource_keys=(key,),
        body_radius_mm=1_200,
        body_half_height_mm=750,
        policy_release=POLICY.policy_version,
        planner_claim=PlannerClaim(verdict="SAFE", policy_version=POLICY.policy_version),
    )


ADJACENT = {"C-01": ("C-02",), "C-02": ("C-01",)}


@pytest.mark.unit
def test_a_pair_that_shares_no_resource_key_is_admitted_by_lane_adjacency() -> None:
    """The gap this closes: C-01 and C-02 are different keys, so a key read never pairs them."""
    subject = plan("plan-subject", lane="C-02", lon=NEAR_LON)
    held = plan("plan-held", lane="C-01")

    selection = select_adjacent_candidates([subject], [held], context(ADJACENT))

    assert [(item.subject, item.plan_version) for item in selection.included] == [
        ("plan-subject", "plan-held")
    ]
    assert "ADJACENT_LANE" in selection.included[0].rules
    assert "SHARED_KEY" not in selection.included[0].rules
    assert "C-02~C-01" in selection.included[0].detail


@pytest.mark.unit
def test_lanes_nobody_declared_are_refused_admission_when_their_volumes_do_not_reach() -> None:
    """The other half of the proof: adjacency is a relation, not a refusal of everything."""
    subject = plan("plan-subject", lane="C-02", lon=NEAR_LON)
    far = plan("plan-far", lane="C-03", lon=FAR_LON)

    assert select_adjacent_candidates([subject], [far], context(ADJACENT)).included == ()
    assert not volumes_reach(subject, far, POLICY)


@pytest.mark.unit
def test_the_map_free_rule_admits_two_lanes_that_only_the_geometry_says_are_too_close() -> None:
    """``OVERLAPPING_VOLUME`` needs no lane map: the checker's own sweep answers for it."""
    subject = plan("plan-subject", lane="C-01")
    stranger = plan("plan-stranger", lane="C-09", minutes=2)

    selection = select_adjacent_candidates([subject], [stranger], context({}))

    assert selection.included[0].rules == ("OVERLAPPING_VOLUME",)


@pytest.mark.unit
def test_a_mission_is_never_its_own_adjacent_committed_record() -> None:
    """A plan's second resource is the same commitment, not evidence of somebody else's.

    Without this exclusion a widened set would convict every multi-resource mission of
    colliding with itself, which is how a well-meant widening turns into a ledger that can
    commit nothing.
    """
    subject = plan("plan-subject", lane="C-01", mission="mission-shared")
    its_own = plan("plan-own-other-resource", lane="C-02", lon=NEAR_LON, mission="mission-shared")
    stranger = plan("plan-stranger", lane="C-02", lon=NEAR_LON, mission="mission-other")

    selection = select_adjacent_candidates([subject], [its_own, stranger], context(ADJACENT))

    assert [item.plan_version for item in selection.included] == ["plan-stranger"]
    assert selection.pool_size == 2


@pytest.mark.unit
def test_cell_adjacency_needs_a_frozen_grid_and_is_silent_without_one() -> None:
    """No grid, no cell size, no invented adjacency.

    ``geom-core`` enumerates a volume cell's neighbourhood, and a run that did not freeze a
    grid has no authority to say which cells touch. The rule declines rather than guessing,
    which leaves the geometric rule as the backstop instead of a fabricated key relation.
    """
    evaluation = context({})
    left = plan("plan-cell-left", cell="V-01")
    right = plan("plan-cell-right", cell="V-02")

    assert evaluation.grid is None
    assert not cells_adjacent(left, right, evaluation)
    assert lane_keys(left) == ()


@pytest.mark.unit
def test_a_record_in_other_minutes_is_not_a_neighbour_in_time() -> None:
    """Adjacent lanes an hour apart are a calendar question, not a conflict question."""
    subject = plan("plan-subject", lane="C-02", lon=NEAR_LON)
    later = plan("plan-later", lane="C-01", minutes=60)

    assert select_adjacent_candidates([subject], [later], context(ADJACENT)).included == ()
