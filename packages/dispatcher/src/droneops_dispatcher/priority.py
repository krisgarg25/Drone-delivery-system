"""Priority semantics: four different concepts, four different powers.

``technical-build/01`` section 12 refuses to let "high priority" be one word. The
contracts enum already separates it; this module is where the separation becomes
executable, as a table of rights. A payment tier and a verified life-critical
mission are both "important" to a human and *not* interchangeable here:

===============================  ===========  ==============  ==================  ==============
concept                          resequence   promote above   propose an          skip fairness
                                 unlaunched   ordinary queue  airborne change     aging
===============================  ===========  ==============  ==================  ==============
``safety_contingency``           yes          yes             yes                 yes
``life_critical``                yes          yes             yes                 no
``commercial_sla``               yes          yes (bounded)   **no**              no
``routine``                      yes          no              no                  no
===============================  ===========  ==============  ==================  ==============

A commercial SLA therefore has no emergency right of way: it may buy a better
position among unlaunched work, and that is the whole of it. The right of way an
airborne aircraft needs is not for sale, and :meth:`PriorityPowers.allows` is the
only gate the rest of the package uses, so no call site can quietly re-define it.

Nothing in this table, or anywhere in the dispatcher, can delete a commitment.
Deletion is not a priority power at all - it belongs to the mission state machine
(``droneops_core_domain.ALLOWED_TRANSITIONS``), and
:func:`withdrawal_is_legal` asks that state machine rather than this file, so an
in-progress mission cannot be cancelled by a dispatcher call even by accident.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from droneops_contracts import MissionStatus, PriorityClass
from droneops_core_domain import ALLOWED_TRANSITIONS

from .errors import CommitmentImmutableError
from .policy import DispatchPolicy, digest_of


class PriorityRight(StrEnum):
    """A single thing a priority class is allowed to do to other people's work."""

    RESEQUENCE_UNLAUNCHED = "resequence_unlaunched"
    PROMOTE_ABOVE_ORDINARY = "promote_above_ordinary"
    PROPOSE_AIRBORNE_CHANGE = "propose_airborne_change"
    SKIP_FAIRNESS_AGING = "skip_fairness_aging"


#: The rights that together amount to an emergency right of way.
EMERGENCY_RIGHTS: frozenset[PriorityRight] = frozenset(
    {PriorityRight.PROPOSE_AIRBORNE_CHANGE, PriorityRight.SKIP_FAIRNESS_AGING}
)

#: Rank 0 goes first. Order is part of the contract: it is the fairness ladder.
PRIORITY_ORDER: tuple[PriorityClass, ...] = (
    PriorityClass.SAFETY_CONTINGENCY,
    PriorityClass.LIFE_CRITICAL,
    PriorityClass.COMMERCIAL_SLA,
    PriorityClass.ROUTINE,
)

_LIFE_CRITICAL_INDEX: int = PRIORITY_ORDER.index(PriorityClass.LIFE_CRITICAL)
_ORDINARY_INDEX: int = PRIORITY_ORDER.index(PriorityClass.ROUTINE)

#: Statuses at which a mission has not launched and may be put in a different order.
UNLAUNCHED_STATUSES: frozenset[MissionStatus] = frozenset(
    {
        MissionStatus.DRAFT,
        MissionStatus.ELIGIBLE,
        MissionStatus.PLANNING,
        MissionStatus.RESERVED_PENDING_COMMIT,
        MissionStatus.ACCEPTED,
    }
)

#: Statuses at which an aircraft is off the ground and the commitment is real.
AIRBORNE_STATUSES: frozenset[MissionStatus] = frozenset(
    {MissionStatus.ACTIVATED, MissionStatus.IN_PROGRESS}
)

#: Statuses whose airspace stays protected even when telemetry is missing (I3).
PROTECTED_STATUSES: frozenset[MissionStatus] = AIRBORNE_STATUSES | {MissionStatus.UNRESOLVED}


@dataclass(frozen=True)
class PriorityPowers:
    """The rights granted to one priority class, as data."""

    priority: PriorityClass
    rights: frozenset[PriorityRight]

    def allows(self, right: PriorityRight) -> bool:
        return right in self.rights

    def has_emergency_right_of_way(self) -> bool:
        return bool(self.rights & EMERGENCY_RIGHTS)

    def rank(self) -> int:
        return PRIORITY_ORDER.index(self.priority)

    def refusal_reason(self, right: PriorityRight) -> str:
        """What to tell the operator when this class may not do that."""
        if self.priority is PriorityClass.COMMERCIAL_SLA and right in EMERGENCY_RIGHTS:
            return (
                "a commercial SLA is a queue position, not an emergency: payment cannot "
                "buy right of way over an airborne aircraft"
            )
        if self.priority is PriorityClass.ROUTINE:
            return "routine work competes through the fairness policy, not through priority"
        return f"{self.priority.value} does not carry the {right.value} right"


#: The table. A test asserts the SLA row cannot reach the emergency column.
PRIORITY_POWERS: Mapping[PriorityClass, PriorityPowers] = {
    PriorityClass.SAFETY_CONTINGENCY: PriorityPowers(
        PriorityClass.SAFETY_CONTINGENCY,
        frozenset(
            {
                PriorityRight.RESEQUENCE_UNLAUNCHED,
                PriorityRight.PROMOTE_ABOVE_ORDINARY,
                PriorityRight.PROPOSE_AIRBORNE_CHANGE,
                PriorityRight.SKIP_FAIRNESS_AGING,
            }
        ),
    ),
    PriorityClass.LIFE_CRITICAL: PriorityPowers(
        PriorityClass.LIFE_CRITICAL,
        frozenset(
            {
                PriorityRight.RESEQUENCE_UNLAUNCHED,
                PriorityRight.PROMOTE_ABOVE_ORDINARY,
                PriorityRight.PROPOSE_AIRBORNE_CHANGE,
            }
        ),
    ),
    PriorityClass.COMMERCIAL_SLA: PriorityPowers(
        PriorityClass.COMMERCIAL_SLA,
        frozenset({PriorityRight.RESEQUENCE_UNLAUNCHED, PriorityRight.PROMOTE_ABOVE_ORDINARY}),
    ),
    PriorityClass.ROUTINE: PriorityPowers(
        PriorityClass.ROUTINE, frozenset({PriorityRight.RESEQUENCE_UNLAUNCHED})
    ),
}


@dataclass(frozen=True)
class MissionStatusRow:
    """One mission as the priority layer needs to see it."""

    mission_id: str
    priority: PriorityClass
    status: MissionStatus
    aircraft_id: str | None = None
    holds_reservation: bool = False

    def is_airborne(self) -> bool:
        return self.status in AIRBORNE_STATUSES

    def is_unlaunched(self) -> bool:
        return self.status in UNLAUNCHED_STATUSES

    def is_protected(self) -> bool:
        return self.is_airborne() or self.holds_reservation or self.status in PROTECTED_STATUSES

    def rank(self) -> int:
        return PRIORITY_ORDER.index(self.priority)


@dataclass(frozen=True)
class ResequenceRefusal:
    """A priority move that did not happen, and why it could not have."""

    requesting_mission_id: str
    target_mission_id: str
    reason: str
    code: str
    target_protected: bool

    def as_row(self) -> tuple[str, str, str, str]:
        return (self.requesting_mission_id, self.target_mission_id, self.code, self.reason)


def powers_of(priority: PriorityClass) -> PriorityPowers:
    """The rights of one class. Unknown classes have no rights at all."""
    return PRIORITY_POWERS[priority]


def priority_rank(priority: PriorityClass) -> int:
    """0 is first. ``life_critical`` is the floor an aging queue may climb to."""
    return PRIORITY_ORDER.index(priority)


def life_critical_floor() -> int:
    """Aging may never push routine work above this rank."""
    return _LIFE_CRITICAL_INDEX


def ordinary_rank() -> int:
    return _ORDINARY_INDEX


def is_airborne(row: MissionStatusRow) -> bool:
    return row.is_airborne()


def withdrawal_is_legal(row: MissionStatusRow) -> bool:
    """Ask the state machine, not the priority table, whether a mission can end.

    A cancelled mission is a transition, and ``IN_PROGRESS -> CANCELLED`` is not in
    ``ALLOWED_TRANSITIONS``: no priority class, payment tier or dispatcher call can
    make an airborne commitment disappear, because the state machine cannot express
    it. That is the structural half of "priority cannot delete an airborne
    commitment".
    """
    return MissionStatus.CANCELLED in ALLOWED_TRANSITIONS[row.status]


def protection_verdict(
    requesting: MissionStatusRow, target: MissionStatusRow, policy: DispatchPolicy
) -> ResequenceRefusal | None:
    """``None`` when this move is allowed; a refusal naming the rule that stopped it."""
    del policy  # the bound lives in the queue layer; protection is not a tuning choice
    if target.is_protected():
        return ResequenceRefusal(
            requesting_mission_id=requesting.mission_id,
            target_mission_id=target.mission_id,
            reason=(
                f"{target.mission_id} is airborne or holds a committed reservation "
                f"({target.status.value}); priority may resequence order, never displace "
                "an aircraft already off the ground"
            ),
            code="airborne_commitment_immutable",
            target_protected=True,
        )
    powers = powers_of(requesting.priority)
    if not powers.allows(PriorityRight.RESEQUENCE_UNLAUNCHED):
        return ResequenceRefusal(
            requesting_mission_id=requesting.mission_id,
            target_mission_id=target.mission_id,
            reason=powers.refusal_reason(PriorityRight.RESEQUENCE_UNLAUNCHED),
            code="no_resequence_right",
            target_protected=False,
        )
    return None


def assert_may_displace(
    requesting: MissionStatusRow, target: MissionStatusRow, policy: DispatchPolicy
) -> ResequenceRefusal:
    """Refusal as an exception, for a caller that tried to *do* the impossible."""
    verdict = protection_verdict(requesting, target, policy)
    if verdict is None:
        raise CommitmentImmutableError(
            f"{target.mission_id} is protected; the caller must produce a proposal instead"
        )
    raise CommitmentImmutableError(f"{verdict.code}: {verdict.reason}")


def airborne_change_authority(requesting: MissionStatusRow) -> ResequenceRefusal | None:
    """May this mission even *ask* for an airborne change? A proposal, never an act."""
    powers = powers_of(requesting.priority)
    if powers.allows(PriorityRight.PROPOSE_AIRBORNE_CHANGE):
        return None
    return ResequenceRefusal(
        requesting_mission_id=requesting.mission_id,
        target_mission_id="",
        reason=powers.refusal_reason(PriorityRight.PROPOSE_AIRBORNE_CHANGE),
        code="no_airborne_change_right",
        target_protected=True,
    )


def powers_digest() -> str:
    """The rights table, hashed: a decision record must say which table it used."""
    rows: list[tuple[str, object]] = []
    for priority in PRIORITY_ORDER:
        powers = PRIORITY_POWERS[priority]
        rights: Sequence[str] = sorted(right.value for right in powers.rights)
        rows.append((priority.value, ",".join(rights)))
    return digest_of(rows)


__all__ = [
    "AIRBORNE_STATUSES",
    "EMERGENCY_RIGHTS",
    "PROTECTED_STATUSES",
    "PRIORITY_ORDER",
    "PRIORITY_POWERS",
    "PriorityPowers",
    "PriorityRight",
    "ResequenceRefusal",
    "UNLAUNCHED_STATUSES",
    "airborne_change_authority",
    "assert_may_displace",
    "is_airborne",
    "life_critical_floor",
    "ordinary_rank",
    "powers_digest",
    "powers_of",
    "priority_rank",
    "protection_verdict",
    "withdrawal_is_legal",
]
