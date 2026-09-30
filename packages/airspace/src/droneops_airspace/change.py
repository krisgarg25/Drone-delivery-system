"""Change handling: a restriction appeared while plans and missions already exist.

The requirement is one row of the failure model, ``technical-build/01`` section 19:

    New restriction -> invalidate unlaunched plans that intersect it; mark airborne
    affected plans.

This module is the boundary where "mark" must not decay into "remove". Three rules are
enforced by the types rather than by convention:

1. A plan that has not launched is *invalidated*: it is recomputed, and recomputation is
   cheap and safe.
2. An airborne mission is *flagged for reconciliation* and its commitment is retained.
   The aircraft is already where the plan said it would be; deleting the record does not
   move it, it only blinds the operator. The model has no operation that drops an
   airborne commitment, and :class:`ChangeImpact` refuses to construct one that claims to.
3. A closed record is history. It is not re-evaluated, rewritten or deleted, because the
   audit trail is the only account of what actually happened.

Stale or unusable data about a *new* restriction is treated as a restriction that may be
live: unlaunched plans are invalidated pending review, and airborne missions are flagged.
"Cannot prove it is safe" is never recorded as "safe".

This package flags and explains; it does not command an aircraft, cancel a reservation or
tell a pilot what to do. Those are other packages' decisions, made on other evidence.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import NoReturn

from pydantic import Field, model_validator

from droneops_contracts.provenance import ContractModel, UtcDatetime

from .eligibility import (
    NON_LAUNCHABLE,
    EligibilityVerdict,
    RouteProfile,
    evaluate_route,
)
from .errors import AirspaceError, refuse_clearance
from .restriction import NOT_A_CLEARANCE, Restriction
from .snapshot import RestrictionSnapshot
from .validity import FreshnessPolicy


class MissionPhase(StrEnum):
    """Where a plan stands with respect to the ground.

    ``UNLAUNCHED`` covers every state before the aircraft leaves: proposed, accepted,
    reserved, committed. Those differ to the planner, not to this question.
    """

    UNLAUNCHED = "unlaunched"
    AIRBORNE = "airborne"
    CLOSED = "closed"


class PlanDisposition(StrEnum):
    UNAFFECTED = "unaffected"
    PLAN_INVALIDATED = "plan_invalidated"
    FLAGGED_FOR_RECONCILIATION = "flagged_for_reconciliation"
    HISTORIC_RECORD_UNTOUCHED = "historic_record_untouched"


class AffectedPlan(ContractModel):
    """A plan as the airspace package can see it: an identity, a phase, a route."""

    plan_id: str = Field(min_length=1)
    mission_id: str = Field(min_length=1)
    phase: MissionPhase
    route: RouteProfile

    @model_validator(mode="after")
    def _check(self) -> AffectedPlan:
        if self.route.plan_id != self.plan_id:
            raise ValueError(
                f"plan id mismatch: impact record says {self.plan_id!r}, route says "
                f"{self.route.plan_id!r}; a change must never be attributed to the wrong plan"
            )
        return self


class PlanImpact(ContractModel):
    """What the new restriction did to one plan, and what it explicitly did not do."""

    plan_id: str
    mission_id: str
    phase: MissionPhase
    disposition: PlanDisposition
    verdict: EligibilityVerdict
    reasons: tuple[str, ...] = ()
    commitment_retained: bool = True
    reconcile_required: bool = False
    note: str = NOT_A_CLEARANCE

    @model_validator(mode="after")
    def _check(self) -> PlanImpact:
        if not self.commitment_retained:
            raise ValueError(
                "this package never removes a commitment; a dropped record is a lost "
                "aircraft in the audit trail"
            )
        if self.phase is MissionPhase.AIRBORNE and (
            self.disposition is PlanDisposition.PLAN_INVALIDATED
        ):
            raise ValueError(
                f"mission {self.mission_id} is airborne: an active commitment cannot be "
                "invalidated out of existence, only flagged for reconciliation"
            )
        if self.phase is MissionPhase.CLOSED and (
            self.disposition is not PlanDisposition.HISTORIC_RECORD_UNTOUCHED
        ):
            raise ValueError(f"mission {self.mission_id} is closed: history is not rewritten")
        return self


class ChangeImpact(ContractModel):
    """The outcome of one new restriction against one set of plans. Total, never partial."""

    restriction_id: str
    impacts: tuple[PlanImpact, ...] = ()
    authoritative: bool = False
    note: str = NOT_A_CLEARANCE

    @model_validator(mode="after")
    def _check(self) -> ChangeImpact:
        ids = [impact.plan_id for impact in self.impacts]
        if len(ids) != len(set(ids)):
            raise ValueError("a plan appears twice in one change impact")
        return self

    @property
    def invalidated(self) -> tuple[PlanImpact, ...]:
        return tuple(i for i in self.impacts if i.disposition is PlanDisposition.PLAN_INVALIDATED)

    @property
    def flagged(self) -> tuple[PlanImpact, ...]:
        return tuple(
            i for i in self.impacts if i.disposition is PlanDisposition.FLAGGED_FOR_RECONCILIATION
        )

    @property
    def untouched(self) -> tuple[PlanImpact, ...]:
        return tuple(i for i in self.impacts if i.disposition is PlanDisposition.UNAFFECTED)

    @property
    def airborne_commitments_dropped(self) -> int:
        """Always zero. Present so a dashboard can assert it rather than hope."""

        return 0

    def impact_for(self, plan_id: str) -> PlanImpact:
        for impact in self.impacts:
            if impact.plan_id == plan_id:
                return impact
        raise AirspaceError(f"plan {plan_id} has no impact in this change: it was not evaluated")

    def as_flight_authorisation(self) -> NoReturn:
        refuse_clearance(f"change impact for restriction {self.restriction_id}")


def _disposition_for(plan: AffectedPlan, verdict: EligibilityVerdict) -> PlanDisposition:
    if plan.phase is MissionPhase.CLOSED:
        return PlanDisposition.HISTORIC_RECORD_UNTOUCHED
    if verdict is EligibilityVerdict.ALLOWED_BY_MODEL:
        return PlanDisposition.UNAFFECTED
    if plan.phase is MissionPhase.UNLAUNCHED:
        return PlanDisposition.PLAN_INVALIDATED
    return PlanDisposition.FLAGGED_FOR_RECONCILIATION


CLOSING_REASON: dict[PlanDisposition, str] = {
    PlanDisposition.PLAN_INVALIDATED: (
        "unlaunched plan intersects the new restriction: invalidated and sent back for "
        "recomputation, which is reversible and cheap"
    ),
    PlanDisposition.FLAGGED_FOR_RECONCILIATION: (
        "airborne mission is affected: flagged for reconciliation with its commitment "
        "intact, because the aircraft is where the plan said it would be"
    ),
    PlanDisposition.HISTORIC_RECORD_UNTOUCHED: "closed mission: an audit record is never rewritten",
    PlanDisposition.UNAFFECTED: "no overlap with the new restriction",
}


def _reasons_for(disposition: PlanDisposition, reasons: Sequence[str]) -> tuple[str, ...]:
    """The evaluated reasons plus one sentence naming the action and why it is that action."""

    base = tuple(dict.fromkeys(reasons))
    return (*base, CLOSING_REASON[disposition])


def evaluate_new_restriction(
    restriction: Restriction,
    plans: Sequence[AffectedPlan],
    *,
    snapshot: RestrictionSnapshot,
    now: UtcDatetime,
    policy: FreshnessPolicy | None = None,
    conditions_satisfied: frozenset[str] = frozenset(),
) -> ChangeImpact:
    """Apply one newly seen restriction to the plans that exist right now.

    Every plan is returned in exactly one disposition. A plan that vanished from the
    result would be a plan nobody is checking, which is the failure this function exists
    to prevent.
    """

    active_policy = policy or FreshnessPolicy()
    single = snapshot.model_copy(update={"restrictions": (restriction,)})
    impacts: list[PlanImpact] = []
    for plan in plans:
        result = evaluate_route(
            plan.route,
            single,
            now=now,
            policy=active_policy,
            conditions_satisfied=conditions_satisfied,
        )
        disposition = _disposition_for(plan, result.verdict)
        blocking = tuple(
            report.reasons[0] for report in result.reports if report.verdict in NON_LAUNCHABLE
        )
        reasons = (
            blocking
            or tuple(report.reasons[0] for report in result.reports)
            or (result.snapshot.reasons if result.snapshot else ())
        )
        impacts.append(
            PlanImpact(
                plan_id=plan.plan_id,
                mission_id=plan.mission_id,
                phase=plan.phase,
                disposition=disposition,
                verdict=result.verdict,
                reasons=_reasons_for(disposition, reasons),
                commitment_retained=True,
                reconcile_required=disposition is PlanDisposition.FLAGGED_FOR_RECONCILIATION,
            )
        )
    return ChangeImpact(
        restriction_id=restriction.restriction_id,
        impacts=tuple(impacts),
        note=NOT_A_CLEARANCE,
    )
