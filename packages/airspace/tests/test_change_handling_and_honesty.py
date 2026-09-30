"""Change handling and the honesty boundary of the package.

Two things are tested here that the spec states as hard rules:

``technical-build/01`` section 19, row "New restriction":
    invalidate unlaunched plans that intersect it; mark airborne affected plans.
    Mark, not remove - and an unlaunched plan is cheap to redo while an airborne
    commitment is a fact about the world.

Section 16 plus conventions section 3: this package is a simulation model. It may not
import the import adapter, may not claim authority, and may not offer a verdict that
reads as a clearance.
"""

from __future__ import annotations

import ast
from datetime import timedelta
from pathlib import Path

import pytest
from airspace_support import (
    CLEAR_ROUTE,
    NOW,
    AffectedPlanFactory,
    evaluate_route_note_probe,
    policy,
    restriction,
    snapshot,
    untrusted_restriction,
)
from pydantic import ValidationError

from droneops_airspace import (
    AUTHORITATIVE_SOURCES,
    EligibilityResult,
    EligibilityVerdict,
    IntersectionReport,
    MissionPhase,
    NotAFlightAuthorisationError,
    PlanDisposition,
    PlanImpact,
    Restriction,
    RestrictionSnapshot,
    RestrictionValidity,
    SnapshotAvailability,
    evaluate_new_restriction,
)

pytestmark = pytest.mark.unit

SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "droneops_airspace"

FORBIDDEN_IMPORTS = (
    "droneops_adapters_airspace_import",
    "droneops_dispatcher",
    "droneops_reservations",
    "droneops_planner",
    "droneops_persistence",
    "httpx",
    "requests",
    "urllib",
    "socket",
    "sqlalchemy",
    "fastapi",
)

CLEARANCE_VOCABULARY = (
    "cleared",
    "clearance",
    "authorized",
    "authorised",
    "approved",
    "permission",
    "granted",
    "licence",
    "license",
    "categorised",
)


def test_unlaunched_plan_intersecting_a_new_restriction_is_invalidated() -> None:

    impact = evaluate_new_restriction(
        restriction(),
        [AffectedPlanFactory(MissionPhase.UNLAUNCHED, "plan-1")],
        snapshot=snapshot(),
        now=NOW,
        policy=policy(),
    )
    (only,) = impact.impacts
    assert only.disposition is PlanDisposition.PLAN_INVALIDATED
    assert only.commitment_retained is True
    assert any("recomputation" in reason for reason in only.reasons)


def test_airborne_mission_is_flagged_never_dropped() -> None:

    impact = evaluate_new_restriction(
        restriction(),
        [AffectedPlanFactory(MissionPhase.AIRBORNE, "plan-2")],
        snapshot=snapshot(),
        now=NOW,
        policy=policy(),
    )
    (only,) = impact.impacts
    assert only.disposition is PlanDisposition.FLAGGED_FOR_RECONCILIATION
    assert only.reconcile_required is True
    assert only.commitment_retained is True
    assert impact.invalidated == ()
    assert impact.airborne_commitments_dropped == 0


def test_a_closed_mission_is_history_and_is_untouched() -> None:

    impact = evaluate_new_restriction(
        restriction(),
        [AffectedPlanFactory(MissionPhase.CLOSED, "plan-3")],
        snapshot=snapshot(),
        now=NOW,
        policy=policy(),
    )
    assert impact.impacts[0].disposition is PlanDisposition.HISTORIC_RECORD_UNTOUCHED


def test_every_plan_receives_exactly_one_disposition() -> None:
    """Totality: a plan missing from the result is a plan nobody is checking."""

    plans = [
        AffectedPlanFactory(MissionPhase.UNLAUNCHED, "p-1"),
        AffectedPlanFactory(MissionPhase.AIRBORNE, "p-2", CLEAR_ROUTE),
        AffectedPlanFactory(MissionPhase.CLOSED, "p-3"),
        AffectedPlanFactory(MissionPhase.UNLAUNCHED, "p-4", CLEAR_ROUTE),
    ]
    impact = evaluate_new_restriction(
        restriction(), plans, snapshot=snapshot(), now=NOW, policy=policy()
    )
    assert {i.plan_id for i in impact.impacts} == {"p-1", "p-2", "p-3", "p-4"}
    assert len(impact.impacts) == len(plans)
    assert {i.plan_id for i in impact.untouched} == {"p-2", "p-4"}


def test_a_stale_new_restriction_is_treated_as_maybe_live() -> None:
    """'Cannot prove it is safe' is never recorded as 'safe'."""

    impact = evaluate_new_restriction(
        untrusted_restriction(),
        [
            AffectedPlanFactory(MissionPhase.UNLAUNCHED, "p-1"),
            AffectedPlanFactory(MissionPhase.AIRBORNE, "p-2"),
        ],
        snapshot=snapshot(),
        now=NOW,
        policy=policy(),
    )
    assert impact.invalidated[0].plan_id == "p-1"
    assert impact.flagged[0].plan_id == "p-2"
    assert impact.untouched == ()


def test_a_restriction_that_arrived_in_a_stale_snapshot_fails_closed() -> None:

    stale_snapshot = snapshot(restriction()).model_copy(
        update={"fetched_at": NOW - timedelta(days=2)}
    )
    impact = evaluate_new_restriction(
        restriction(),
        [AffectedPlanFactory(MissionPhase.AIRBORNE, "p-1")],
        snapshot=stale_snapshot,
        now=NOW,
        policy=policy(),
    )
    assert impact.impacts[0].disposition is PlanDisposition.FLAGGED_FOR_RECONCILIATION
    assert impact.impacts[0].verdict is EligibilityVerdict.NO_LAUNCH_INSUFFICIENT_DATA


def test_the_type_refuses_an_airborne_invalidation() -> None:
    with pytest.raises(ValidationError, match="airborne"):
        PlanImpact(
            plan_id="p-1",
            mission_id="m-1",
            phase=MissionPhase.AIRBORNE,
            disposition=PlanDisposition.PLAN_INVALIDATED,
            verdict=EligibilityVerdict.BLOCKED_NOW,
        )


def test_the_type_refuses_a_dropped_commitment() -> None:
    with pytest.raises(ValidationError, match="never removes a commitment"):
        PlanImpact(
            plan_id="p-1",
            mission_id="m-1",
            phase=MissionPhase.UNLAUNCHED,
            disposition=PlanDisposition.PLAN_INVALIDATED,
            verdict=EligibilityVerdict.BLOCKED_NOW,
            commitment_retained=False,
        )


def test_no_plan_is_evaluated_twice() -> None:

    with pytest.raises(ValidationError, match="appears twice"):
        evaluate_new_restriction(
            restriction(),
            [
                AffectedPlanFactory(MissionPhase.UNLAUNCHED, "same"),
                AffectedPlanFactory(MissionPhase.UNLAUNCHED, "same"),
            ],
            snapshot=snapshot(),
            now=NOW,
            policy=policy(),
        ).model_dump()


def test_change_impact_refuses_to_be_read_as_clearance() -> None:

    impact = evaluate_new_restriction(
        restriction(),
        [AffectedPlanFactory(MissionPhase.UNLAUNCHED, "p-1")],
        snapshot=snapshot(),
        now=NOW,
        policy=policy(),
    )
    with pytest.raises(NotAFlightAuthorisationError):
        impact.as_flight_authorisation()


def test_the_package_never_imports_the_import_adapter() -> None:
    """Section 16 is structural: airspace evaluates observations, it does not fetch them."""

    offenders: list[str] = []
    for source in sorted(SRC_ROOT.glob("*.py")):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                for forbidden in FORBIDDEN_IMPORTS:
                    if name == forbidden or name.startswith(f"{forbidden}."):
                        offenders.append(f"{source.name}: import {name}")
    assert offenders == []


def test_no_wall_clock_or_filesystem_in_the_decision_path() -> None:
    for source in sorted(SRC_ROOT.glob("*.py")):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[0] not in {"time", "os", "random", "pathlib"}, (
                        f"{source.name} imports {alias.name}: the clock is injected, never read"
                    )


def test_no_verdict_reads_as_a_clearance() -> None:
    values = [verdict.value for verdict in EligibilityVerdict]
    assert all(not any(word in value for word in CLEARANCE_VOCABULARY) for value in values)
    assert "allowed_by_model" in values, "the only permissive verdict is explicitly scoped"


def test_no_field_is_named_like_an_authorisation() -> None:
    for model in (Restriction, RestrictionValidity, EligibilityResult, IntersectionReport):
        for field_name in model.model_fields:
            normalised = field_name.replace("authoritative", "")
            assert not any(word in normalised for word in CLEARANCE_VOCABULARY), (
                f"{model.__name__}.{field_name} reads as an authorisation"
            )


def test_authoritative_source_registry_is_empty_so_nothing_may_claim_authority() -> None:
    assert frozenset() == AUTHORITATIVE_SOURCES
    with pytest.raises(ValidationError, match="registered as authoritative"):
        RestrictionSnapshot(
            snapshot_id="snap-fake",
            source="digitalsky",
            fetched_at=NOW,
            availability=SnapshotAvailability.AUTHORITATIVE_FEED,
            authoritative=True,
        )


def test_a_no_feed_snapshot_may_not_quietly_carry_restrictions() -> None:
    with pytest.raises(ValidationError, match="contradiction"):
        RestrictionSnapshot.no_feed(NOW).model_copy(update={}).model_validate(
            {
                **RestrictionSnapshot.no_feed(NOW).model_dump(),
                "restrictions": [restriction().model_dump()],
            }
        )


def test_every_public_verdict_carries_the_simulation_note() -> None:
    assert evaluate_route_note_probe().startswith("Simulation model only")
