"""Steps 3 to 6, as pure functions: rank, generate, reject restricted, check energy.

These four steps are the part of ``technical-build/01`` section 9 that can be proven
without a database, so they live here rather than beside the SQL. The ledger calls
:data:`preflight` inside the transaction and persists exactly what it returns - the
ranking, the rejected candidates and their codes - because a decision record that only
stores the winner cannot answer the Phase 4 question ("why not the other one?").

Determinism, and why it is a safety property
    The rank key is ``(total_units, lateness_us, energy_margin_wh desc, aircraft_id)``
    and the candidate key is ``(total_units, strategy, candidate_id)``. Every one of
    those fields is either an integer or a string, and the final tie-break is always
    the identifier, so the ordering is a *total* order: two runs over the same inputs
    produce the same winner, and a replay of a rejected request produces the same
    rejection. Ordering by insertion order, dict order or a float-only score would
    make "which aircraft got the corridor" depend on something nobody recorded.

Eligibility excludes, it does not demote
    ``maintenance`` and ``offline`` aircraft are removed by :func:`eligible_aircraft`
    before ranking, so they cannot appear in the trace's eligible list at all
    (invariant I5: the candidate *query* excludes them). Connectivity is orthogonal to
    availability, and a ``lost`` or ``unknown`` link makes the aircraft's own position
    uncertain - uncertain aircraft do not get new airspace.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum

from droneops_contracts import AltitudeReference, AircraftStatus, LinkQuality, PriorityClass

from .errors import Rejection, RejectionCode
from .protocol import ResourceClaim, find_self_conflict, in_lock_order, lock_tokens

OPERABLE_AIRCRAFT: frozenset[AircraftStatus] = frozenset(
    {
        AircraftStatus.AVAILABLE,
        AircraftStatus.RESERVED,
        AircraftStatus.CHARGING,
        AircraftStatus.LOADING,
        AircraftStatus.UNLOADING,
    }
)
"""Statuses that can be handed a new commitment. ``flying`` is absent deliberately -
an airborne aircraft cannot be double-committed onto a new corridor."""

RELIABLE_LINK: frozenset[LinkQuality] = frozenset({LinkQuality.GOOD, LinkQuality.DEGRADED})


class Verdict(StrEnum):
    """The three answers, spelled exactly as the independent checker spells them."""

    SAFE = "SAFE"
    CONFLICT = "CONFLICT"
    UNPROVEN = "UNPROVEN"

    @property
    def blocks_release(self) -> bool:
        """Only ``SAFE`` releases. ``UNPROVEN`` is unknown, and unknown is not safe."""
        return self is not Verdict.SAFE


@dataclass(frozen=True)
class EnergyVerdict:
    """The energy and recovery numbers for one candidate, as integer watt-hours."""

    available_wh: int
    consumed_wh: int
    reserve_floor_wh: int
    recovery_margin_wh: int
    recovery_feasible: bool
    model_version: str

    @property
    def margin_wh(self) -> int:
        """What is left above the floor after the whole mission, recovery included."""
        return self.available_wh - self.consumed_wh - self.reserve_floor_wh

    def gate(self) -> Rejection | None:
        """I4: outbound + handover + reserve + recovery, or nothing at all.

        ``recovery_margin_wh`` is reported rather than folded into a boolean, so the
        trace shows how far short a rejected candidate was.
        """
        if not self.recovery_feasible:
            return Rejection(
                RejectionCode.RECOVERY_NOT_FEASIBLE,
                f"recovery margin {self.recovery_margin_wh} Wh under model {self.model_version}",
            )
        if self.margin_wh < 0:
            return Rejection(
                RejectionCode.ENERGY_MARGIN_BELOW_RESERVE,
                (
                    f"margin {self.margin_wh} Wh below the {self.reserve_floor_wh} Wh floor "
                    f"under model {self.model_version}"
                ),
            )
        return None


@dataclass(frozen=True)
class ProfileSample:
    """One raw 4D plan point - the payload an independent checker re-derives volumes from.

    The altitude reference travels with the number (invariant I9). A sample without a
    reference is not a sample the checker can use, so the field is required and
    ``UNSPECIFIED`` is refused by :meth:`altitude_is_referenceable`.
    """

    at: datetime
    lat_deg: float
    lon_deg: float
    altitude_m: float
    altitude_reference: AltitudeReference
    speed_mm_per_s: int = 0

    def altitude_is_referenceable(self) -> bool:
        return self.altitude_reference is not AltitudeReference.UNSPECIFIED

    def as_row(self) -> tuple[str, float, float, float, str, int]:
        return (
            self.at.isoformat(),
            self.lat_deg,
            self.lon_deg,
            self.altitude_m,
            self.altitude_reference.value,
            self.speed_mm_per_s,
        )


@dataclass(frozen=True)
class AircraftCandidate:
    """One aircraft offered to the ledger, with the numbers that rank it."""

    aircraft_id: str
    status: AircraftStatus
    link: LinkQuality
    priority_class: PriorityClass
    energy: EnergyVerdict
    lateness_us: int
    disturbance_units: int
    fairness_units: int
    battery_pct: int = 0

    @property
    def total_units(self) -> int:
        """The single ranked score: late, disturbing and unfair cost the same way."""
        return self.lateness_us + self.disturbance_units + self.fairness_units

    def rank_key(self) -> tuple[int, int, int, int, str]:
        return (
            self.total_units,
            self.lateness_us,
            -self.energy.margin_wh,
            _PRIORITY_RANK[self.priority_class],
            self.aircraft_id,
        )

    def eligibility(self) -> Rejection | None:
        """The reason this aircraft cannot be offered airspace, or ``None``."""
        if self.status not in OPERABLE_AIRCRAFT:
            return Rejection(
                RejectionCode.AIRCRAFT_NOT_OPERABLE,
                f"status {self.status.value} cannot be assigned a new commitment",
                aircraft_id=self.aircraft_id,
            )
        if self.link not in RELIABLE_LINK:
            return Rejection(
                RejectionCode.AIRCRAFT_NOT_OPERABLE,
                f"link {self.link.value} makes the aircraft position uncertain",
                aircraft_id=self.aircraft_id,
            )
        return self.energy.gate()


_PRIORITY_RANK: dict[PriorityClass, int] = {
    priority: index
    for index, priority in enumerate(
        (
            PriorityClass.SAFETY_CONTINGENCY,
            PriorityClass.LIFE_CRITICAL,
            PriorityClass.COMMERCIAL_SLA,
            PriorityClass.ROUTINE,
        )
    )
}


@dataclass(frozen=True)
class CandidatePlan:
    """One route the planner proposes for one aircraft: a claim set plus its evidence."""

    candidate_id: str
    aircraft_id: str
    strategy: str
    route_digest: str
    claims: tuple[ResourceClaim, ...]
    profile: tuple[ProfileSample, ...]
    planner_verdict: Verdict = Verdict.SAFE
    config_release_sha: str = ""
    allow_rule_id: str | None = None
    allow_rule_version: str | None = None

    def ordered_claims(self) -> tuple[ResourceClaim, ...]:
        return in_lock_order(self.claims)

    def lock_order(self) -> tuple[str, ...]:
        return lock_tokens(self.claims)

    def profile_is_referenceable(self) -> bool:
        """Every sample carries a usable altitude reference, or the checker cannot vote."""
        return bool(self.profile) and all(
            sample.altitude_is_referenceable() for sample in self.profile
        )


@dataclass(frozen=True)
class WorldView:
    """The frozen inputs step 2 produced, narrowed to what steps 5 and 8 need.

    ``snapshot_digest`` is the canonical hash of exactly the rows read inside the
    transaction. A commit that cannot name it did not freeze anything, so it is
    refused at step 2 rather than guessed at later.
    """

    policy_version: str
    config_release_sha: str
    snapshot_digest: str
    reservation_version: int
    restricted_keys: frozenset[str] = frozenset()
    unavailable: tuple[tuple[str, str], ...] = ()
    offline: bool = False

    def status_of(self, identity: str) -> str | None:
        for key, status in self.unavailable:
            if key == identity:
                return status
        return None

    def gate(self) -> Rejection | None:
        """Section 19 "Outage": no new commitments while the world is unreconciled."""
        if not self.snapshot_digest:
            return Rejection(
                RejectionCode.SNAPSHOT_NOT_FROZEN,
                "no snapshot digest: the inputs were not frozen inside the transaction",
            )
        if self.offline:
            return Rejection(
                RejectionCode.RESOURCE_UNAVAILABLE,
                "the ledger is in outage; reconcile actual state before accepting new work",
            )
        return None


@dataclass(frozen=True)
class CommitCommand:
    """One commit attempt: an identity, a world, aircraft, and candidate plans.

    ``commitment_token`` is the idempotency identity (conventions section 8). It is
    the same value for a retry of the same request, which is what makes step 1 able to
    return the original mission instead of a second commitment and a second charge.
    """

    tenant_id: uuid.UUID
    order_id: uuid.UUID
    mission_id: uuid.UUID
    plan_version_id: uuid.UUID
    commitment_token: str
    idempotency_key: str
    policy_version: str
    config_release_sha: str
    actor: str
    world: WorldView
    aircraft: tuple[AircraftCandidate, ...]
    candidates: tuple[CandidatePlan, ...]
    expected_reservation_version: int | None = None
    request_id: str = ""
    priority_class: PriorityClass = PriorityClass.ROUTINE
    allow_rule_id: str | None = None
    allow_rule_version: str | None = None

    def with_world(self, world: WorldView) -> CommitCommand:
        """This command re-stated against the world the ledger actually froze.

        Steps 3 to 6 must run on the merged view (the caller's declaration plus what the
        catalog and the corridor table said), not on the caller's assumption. Returning a
        new frozen command rather than mutating keeps the trace's input hash honest.
        """
        return replace(self, world=world)

    def named_allow_rule(self) -> tuple[str, str] | None:
        """The exception this request claims, only if it is both named *and* versioned.

        An unnamed or unversioned "exception" is not one: it cannot be checked against
        the checker's own rule set, so the ledger treats it as no rule at all.
        """
        if self.allow_rule_id and self.allow_rule_version:
            return (self.allow_rule_id, self.allow_rule_version)
        return None

    def identity(self) -> tuple[str, str, str]:
        """(tenant, order, token) - the triple a duplicate is matched on."""
        return (str(self.tenant_id), str(self.order_id), self.commitment_token)


@dataclass(frozen=True)
class Selection:
    """Steps 3 to 6 done: one candidate, one aircraft, and the full rejection list."""

    candidate: CandidatePlan
    aircraft: AircraftCandidate
    eligible: tuple[AircraftCandidate, ...]
    rejected: tuple[Rejection, ...]
    ordered_claims: tuple[ResourceClaim, ...]
    lock_order: tuple[str, ...]
    steps_recorded: tuple[str, ...]
    tried: tuple[tuple[str, str], ...] = ()

    def ranking_rows(self) -> tuple[tuple[str, int, int, int], ...]:
        """``(aircraft_id, total_units, lateness_us, margin_wh)``, best first.

        Stored verbatim in ``decision_record.eligible_candidates_json`` so Phase 4 can
        show the order the ledger actually tried, not a re-derivation of it.
        """
        return tuple(
            (
                aircraft.aircraft_id,
                aircraft.total_units,
                aircraft.lateness_us,
                aircraft.energy.margin_wh,
            )
            for aircraft in self.eligible
        )


@dataclass(frozen=True)
class PreflightFailure:
    """A reasoned refusal from steps 3 to 6, with everything already rejected."""

    rejection: Rejection
    eligible: tuple[AircraftCandidate, ...]
    rejected: tuple[Rejection, ...]


def eligible_aircraft(
    aircraft: Iterable[AircraftCandidate],
) -> tuple[tuple[AircraftCandidate, ...], tuple[Rejection, ...]]:
    """Step 3, first half: filter, then rank. Returns ``(eligible, rejections)``."""
    kept: list[AircraftCandidate] = []
    rejections: list[Rejection] = []
    for candidate in aircraft:
        reason = candidate.eligibility()
        if reason is None:
            kept.append(candidate)
        else:
            rejections.append(reason)
    return tuple(sorted(kept, key=AircraftCandidate.rank_key)), tuple(
        sorted(rejections, key=lambda item: (item.aircraft_id or "", item.code.value))
    )


def rank_candidates(
    candidates: Iterable[CandidatePlan], aircraft_rank: Mapping[str, int]
) -> tuple[tuple[CandidatePlan, ...], tuple[Rejection, ...]]:
    """Step 4: keep only plans whose aircraft is still eligible, in stable order.

    The primary key is the aircraft's rank (so the best aircraft's route is tried
    first), then the strategy name and candidate id as declared tie-breaks. A plan
    offered on an aircraft that step 3 filtered out is reported, not silently dropped:
    that is the case an operator asks about afterwards.
    """
    kept: list[tuple[int, CandidatePlan]] = []
    rejections: list[Rejection] = []
    for plan in candidates:
        rank = aircraft_rank.get(plan.aircraft_id)
        if rank is None:
            rejections.append(
                Rejection(
                    RejectionCode.AIRCRAFT_NOT_OPERABLE,
                    f"candidate {plan.candidate_id} was planned on ineligible aircraft "
                    f"{plan.aircraft_id}",
                    candidate_id=plan.candidate_id,
                    aircraft_id=plan.aircraft_id,
                )
            )
            continue
        if not plan.claims:
            rejections.append(
                Rejection(
                    RejectionCode.LOCK_ORDER_INVALID,
                    f"candidate {plan.candidate_id} claims nothing",
                    candidate_id=plan.candidate_id,
                )
            )
            continue
        kept.append((rank, plan))
    ordered = sorted(kept, key=lambda item: (item[0], item[1].strategy, item[1].candidate_id))
    return (
        tuple(plan for _, plan in ordered),
        tuple(sorted(rejections, key=lambda item: item.candidate_id or "")),
    )


def reject_restricted(
    plan: CandidatePlan, world: WorldView
) -> tuple[ResourceClaim, ...] | Rejection:
    """Step 5: a candidate that touches restricted or unavailable airspace is out."""
    ordered = plan.ordered_claims()
    self_conflict = find_self_conflict(plan.claims)
    if self_conflict is not None:
        return _with_candidate(self_conflict, plan)
    for claim in ordered:
        if claim.identity in world.restricted_keys:
            return _with_candidate(
                Rejection(
                    RejectionCode.CANDIDATE_RESTRICTED,
                    f"{claim.identity} is restricted under policy {world.policy_version}",
                    resource_key=claim.stored_key,
                    candidate_id=plan.candidate_id,
                ),
                plan,
            )
        status = world.status_of(claim.identity)
        if status is not None and status != "available":
            return _with_candidate(
                Rejection(
                    RejectionCode.RESOURCE_UNAVAILABLE,
                    f"{claim.identity} has capacity status {status}",
                    resource_key=claim.stored_key,
                    candidate_id=plan.candidate_id,
                ),
                plan,
            )
    return ordered


def _with_candidate(rejection: Rejection, plan: CandidatePlan) -> Rejection:
    return Rejection(
        rejection.code,
        rejection.detail,
        resource_key=rejection.resource_key,
        candidate_id=plan.candidate_id,
        aircraft_id=plan.aircraft_id,
    )


def preflight(command: CommitCommand) -> Selection | PreflightFailure:
    """Steps 3, 4, 5 and 6 in order, with the running rejection list preserved.

    The first *eligible* candidate that clears restriction and energy wins; the ones
    tried before it are reported as rejections, not discarded. Trying every candidate
    and reporting only the winner would make the trace unexplanable.
    """
    rejected: list[Rejection] = []
    world_gate = command.world.gate()
    if world_gate is not None:
        return PreflightFailure(world_gate, (), (world_gate,))

    eligible, aircraft_rejections = eligible_aircraft(command.aircraft)
    rejected.extend(aircraft_rejections)
    if not eligible:
        rejection = Rejection(
            RejectionCode.NO_ELIGIBLE_AIRCRAFT,
            f"{len(aircraft_rejections)} aircraft offered, none operable",
        )
        return PreflightFailure(rejection, (), tuple(rejected) + (rejection,))

    aircraft_rank = {
        aircraft.aircraft_id: index for index, aircraft in enumerate(eligible)
    }
    ranked_plans, plan_rejections = rank_candidates(command.candidates, aircraft_rank)
    rejected.extend(plan_rejections)
    if not ranked_plans:
        rejection = Rejection(
            RejectionCode.NO_CANDIDATE_PLANS,
            "no candidate plan survived eligibility and claim validation",
        )
        return PreflightFailure(rejection, eligible, tuple(rejected) + (rejection,))

    by_id = {aircraft.aircraft_id: aircraft for aircraft in eligible}
    for plan in ranked_plans:
        outcome = reject_restricted(plan, command.world)
        if isinstance(outcome, Rejection):
            rejected.append(outcome)
            continue
        aircraft = by_id[plan.aircraft_id]
        energy = aircraft.energy.gate()
        if energy is not None:
            rejected.append(
                Rejection(
                    energy.code,
                    energy.detail,
                    candidate_id=plan.candidate_id,
                    aircraft_id=plan.aircraft_id,
                )
            )
            continue
        if not plan.profile_is_referenceable():
            rejected.append(
                Rejection(
                    RejectionCode.CHECKER_UNPROVEN,
                    f"candidate {plan.candidate_id} has no usable altitude reference; "
                    "the checker would be unable to vote",
                    candidate_id=plan.candidate_id,
                    aircraft_id=plan.aircraft_id,
                )
            )
            continue
        return Selection(
            candidate=plan,
            aircraft=aircraft,
            eligible=eligible,
            rejected=tuple(rejected),
            ordered_claims=outcome,
            lock_order=plan.lock_order(),
            steps_recorded=(
                "rank_eligible_aircraft",
                "generate_candidate_routes",
                "reject_restricted_candidates",
                "apply_energy_and_recovery_checks",
            ),
            tried=tuple((plan.candidate_id, plan.aircraft_id) for plan in ranked_plans),
        )

    return PreflightFailure(
        rejected[-1] if rejected else Rejection(RejectionCode.NO_CANDIDATE_PLANS, "nothing to try"),
        eligible,
        tuple(rejected),
    )


__all__ = [
    "OPERABLE_AIRCRAFT",
    "RELIABLE_LINK",
    "AircraftCandidate",
    "CandidatePlan",
    "CommitCommand",
    "EnergyVerdict",
    "PreflightFailure",
    "ProfileSample",
    "Selection",
    "Verdict",
    "eligible_aircraft",
    "eligible_ids",
    "rank_candidates",
    "reject_restricted",
    "preflight",
]


def eligible_ids(selection: Selection) -> frozenset[str]:
    """The aircraft identifiers the ledger was willing to consider, sorted."""
    return frozenset(aircraft.aircraft_id for aircraft in selection.eligible)
