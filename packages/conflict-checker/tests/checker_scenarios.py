"""Record builders shared by the checker's own suite.

A module rather than ``conftest`` because every package in this monorepo has a
``conftest`` and only one can own that import name in a full-suite run. The
builders deliberately *copy nothing* from the planner or ``geom-4d``: they take
geodetic metres and UTC datetimes, which is all a committed ledger row has to give.

The margins in play come from the real policy file - 15 m horizontal, 10 m vertical,
5 m position uncertainty, 5 s timing, 10 s transition - so a body of radius 0.25 m
and half height 0.15 m protects 20.25 m laterally and 10.15 m vertically. A leg is
padded with timing uncertainty (5 s) and, only if its altitude changes, with the
transition buffer as well (15 s): the rule the policy file states and the planner's
builder applies, so the checker's extra reach over the planner is its 1 mm contact
tolerance, its held heads and tails and its two time-blind inspections - never a
different reading of the same two margins. Distances below are chosen against those
numbers, not against a tolerance someone tuned until the test went green.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from droneops_conflict_checker import (
    CheckerPolicy,
    CommittedPlanRecord,
    EvaluationContext,
    PlannerClaim,
    PlanPoint,
    load_policy_file,
)
from droneops_geom_core import GeoPoint, destination_point

REPO_ROOT = Path(__file__).resolve().parents[3]
POLICY_PATH = REPO_ROOT / "contracts" / "configuration" / "protected_volume_policy.yaml"

#: One origin, so bearings and distances in every test mean the same place.
ORIGIN = GeoPoint(30.7387, 76.7687)

#: When every scenario starts, and when the checker is asked to evaluate it.
T0 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
EVALUATED_AT = T0 + timedelta(hours=1)

BODY_RADIUS_MM = 250
BODY_HALF_HEIGHT_MM = 150


def policy() -> CheckerPolicy:
    """The real, versioned policy file - never a hand-tuned stand-in."""
    return load_policy_file(POLICY_PATH)


def context(
    *,
    adjacency: dict[str, tuple[str, ...]] | None = None,
    evaluated_at: datetime = EVALUATED_AT,
    telemetry_budget_us: int = 30_000_000,
    grid=None,
    override_policy: CheckerPolicy | None = None,
) -> EvaluationContext:
    return EvaluationContext(
        policy=override_policy or policy(),
        evaluated_at=evaluated_at,
        adjacency=adjacency or {},
        telemetry_budget_us=telemetry_budget_us,
        grid=grid,
    )


def place(east_m: float, north_m: float) -> GeoPoint:
    """A geodetic point at ``(east, north)`` metres off the shared origin."""
    step = destination_point(ORIGIN, 90.0 if east_m >= 0 else 270.0, abs(east_m))
    return destination_point(step, 0.0 if north_m >= 0 else 180.0, abs(north_m))


def point(
    east_m: float,
    north_m: float,
    *,
    altitude_m: float = 40.0,
    at_s: float = 0.0,
    reference: str | None = "AGL",
    origin: datetime = T0,
    speed_mm_per_s: int = 10_000,
) -> PlanPoint:
    geo = place(east_m, north_m)
    return PlanPoint(
        at=origin + timedelta(seconds=at_s),
        lat_deg=geo.lat_deg,
        lon_deg=geo.lon_deg,
        altitude_m=altitude_m,
        altitude_reference=reference,
        speed_mm_per_s=speed_mm_per_s,
    )


def record(
    plan_version: str,
    points: list[PlanPoint],
    lanes: list[str] | None = None,
    *,
    status: str = "committed",
    claim: str = "SAFE",
    rule_id: str | None = None,
    rule_version: str | None = None,
    release: str | None = None,
    radius_mm: int = BODY_RADIUS_MM,
    half_height_mm: int = BODY_HALF_HEIGHT_MM,
    telemetry_last_fix: datetime | None = None,
    restriction_expires_at: datetime | None = EVALUATED_AT + timedelta(days=7),
    policy_active: str = "sim-policy-v1",
) -> CommittedPlanRecord:
    """One committed record, with the provenance a ledger row would actually carry."""
    return CommittedPlanRecord(
        plan_version=plan_version,
        mission_id=plan_version.lower(),
        status=status,
        points=tuple(points),
        resource_keys=tuple(
            lane if ":" in lane else f"corridor:{lane}" for lane in (lanes or ["SEG-01"])
        ),
        body_radius_mm=radius_mm,
        body_half_height_mm=half_height_mm,
        policy_release=release or policy_active,
        telemetry_last_fix=telemetry_last_fix,
        restriction_expires_at=restriction_expires_at,
        planner_claim=PlannerClaim(
            verdict=claim,
            policy_version=policy_active,
            allow_rule_id=rule_id,
            allow_rule_version=rule_version,
        ),
    )


def straight(plan_version: str, east_from: float, east_to: float, *, at_s: float = 0.0,
             altitude_m: float = 40.0, lanes: list[str] | None = None, **kwargs) -> CommittedPlanRecord:
    """A plan that flies a straight east-west leg in ten seconds."""
    return record(
        plan_version,
        [point(east_from, 0.0, at_s=at_s, altitude_m=altitude_m),
         point(east_to, 0.0, at_s=at_s + 10.0, altitude_m=altitude_m)],
        lanes,
        **kwargs,
    )
