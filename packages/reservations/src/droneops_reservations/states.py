"""Reservation state, uncertainty, and who is allowed to erase airspace.

conventions section 9::

    tentative -> committed -> activated -> completed | cancelled | unresolved

``unresolved`` exists precisely because an uncertain outcome does not mean the
airspace became empty. That single sentence is the reason this module is separate
from the ordering logic: every question the ledger asks about a state is really the
question *"does this row still protect space?"*, and the answer is not
``state != cancelled``.

The three questions, stated once
    :func:`protects_space`
        ``committed``, ``activated`` and ``unresolved`` all hold the resource.
        ``unresolved`` holds it *indefinitely* - until a human or a reconciliation
        process resolves it, never until a timer expires. This is invariant I3.
    :func:`blocks_new_commitment`
        what a concurrent commit must see and refuse. It equals
        :func:`protects_space` for every state except ``tentative``: a tentative row
        belongs to an in-flight transaction that has not promoted itself yet, so it
        blocks nothing - the canonical resource row lock is what serialises it.
    :func:`may_be_released`
        the states a *priority* preemption or a cancellation may move to
        ``cancelled``. ``activated`` is absent: priority may never delete an
        airborne commitment (Phase 3 exit criterion), and ``unresolved`` is absent
        because resolving uncertainty is a reconciliation, not a release.

The database is a partial guard and this file is the rest of it
    ``no_committed_overlap`` indexes only ``('committed', 'activated')``. An
    ``unresolved`` row is deliberately outside that predicate - it is neither free
    nor held by a live commitment - so nothing in PostgreSQL would stop a new
    overlapping commitment. The ledger's step-8 recheck consults
    :data:`PROTECTED_STATES` and refuses, which is the only place I3 is enforced.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum

from droneops_contracts import MissionStatus, ReservationState

from .errors import Rejection, RejectionCode, ReservationTransitionError


class CommitAuthority(StrEnum):
    """Who may write committed airspace. There is exactly one answer."""

    LEDGER = "reservation_ledger"


class UnresolvedHandling(StrEnum):
    """What the ledger does with an uncertain outcome. Never ``release``."""

    PROTECT = "protect"
    RECONCILE = "reconcile"


COMMITTED_STATES: frozenset[ReservationState] = frozenset(
    {ReservationState.COMMITTED, ReservationState.ACTIVATED}
)
"""The states the database's exclusion constraint indexes."""

UNRESOLVED_STATES: frozenset[ReservationState] = frozenset({ReservationState.UNRESOLVED})
"""Uncertain outcomes: protected, reconcilable, and invisible to the index."""

PROTECTED_STATES: frozenset[ReservationState] = COMMITTED_STATES | UNRESOLVED_STATES
"""Everything that still holds space. A new commitment must yield to all of it."""

RELEASED_STATES: frozenset[ReservationState] = frozenset(
    {ReservationState.CANCELLED, ReservationState.COMPLETED}
)
"""The only states that genuinely empty a volume."""

RESERVATION_TRANSITIONS: dict[ReservationState, frozenset[ReservationState]] = {
    ReservationState.TENTATIVE: frozenset({ReservationState.COMMITTED, ReservationState.CANCELLED}),
    ReservationState.COMMITTED: frozenset(
        {ReservationState.ACTIVATED, ReservationState.CANCELLED, ReservationState.UNRESOLVED}
    ),
    ReservationState.ACTIVATED: frozenset(
        {ReservationState.COMPLETED, ReservationState.UNRESOLVED}
    ),
    # Reconciliation resolves uncertainty in one of three directions, and each of
    # them is a *decision*, never a timeout.
    ReservationState.UNRESOLVED: frozenset(
        {ReservationState.ACTIVATED, ReservationState.COMPLETED, ReservationState.CANCELLED}
    ),
    ReservationState.COMPLETED: frozenset(),
    ReservationState.CANCELLED: frozenset(),
}
"""Append-only corrections: a terminal state never re-enters the machine."""

_MISSION_COMMIT_HOPS: tuple[MissionStatus, ...] = (
    MissionStatus.DRAFT,
    MissionStatus.ELIGIBLE,
    MissionStatus.PLANNING,
    MissionStatus.RESERVED_PENDING_COMMIT,
    MissionStatus.ACCEPTED,
)
"""The only path by which a mission reaches ``accepted``.

``planning -> activated`` is impossible (conventions section 9) and the ledger stops
at ``accepted``: activation is a later, separate authority step, which is exactly the
point of ``reserved_pending_commit``.
"""


def protects_space(state: ReservationState | str) -> bool:
    """Whether a row in this state still holds its resource window."""
    return _state(state) in PROTECTED_STATES


def blocks_new_commitment(state: ReservationState | str) -> bool:
    """Whether a concurrent commit must refuse this row."""
    return _state(state) in PROTECTED_STATES


def may_be_released(state: ReservationState | str) -> bool:
    """Whether a cancellation or a priority preemption may cancel this row.

    ``tentative`` is releasable because it was never committed. ``activated`` and
    ``unresolved`` are not: the first is airborne, the second is unknown, and neither
    becomes empty airspace because someone wanted it to be.
    """
    return _state(state) in {ReservationState.TENTATIVE, ReservationState.COMMITTED}


def is_terminal(state: ReservationState | str) -> bool:
    return not RESERVATION_TRANSITIONS[_state(state)]


def assert_transition(source: ReservationState | str, target: ReservationState | str) -> None:
    """Raise unless the state machine allows ``source -> target``.

    Raises:
        ReservationTransitionError: an illegal hop, naming both states. A skipped
            state (``tentative -> activated``) is the bug this guards against.
    """
    first, second = _state(source), _state(target)
    if second not in RESERVATION_TRANSITIONS[first]:
        raise ReservationTransitionError(f"illegal reservation transition: {first.value} -> {second.value}")


def protected_states() -> tuple[str, ...]:
    """The state labels a recheck query must look for, sorted for SQL stability."""
    return tuple(sorted(state.value for state in PROTECTED_STATES))


def releasable_states() -> tuple[str, ...]:
    """The state labels a cancellation may transition from."""
    return tuple(
        sorted(state.value for state in RESERVATION_TRANSITIONS if may_be_released(state))
    )


def mission_commit_path() -> tuple[MissionStatus, ...]:
    """The hop-by-hop path the ledger walks a mission along when it commits."""
    return _MISSION_COMMIT_HOPS


def assert_mission_can_be_accepted(status: MissionStatus | str) -> None:
    """A mission may only be ``accepted`` after passing through the ledger.

    Raises:
        ReservationTransitionError: the caller tried to reach ``accepted`` or
            ``activated`` without the commit hop, i.e. ``planning -> activated``.
    """
    current = MissionStatus(status)
    if current is MissionStatus.ACCEPTED:
        return
    if current is MissionStatus.ACTIVATED:
        raise ReservationTransitionError(
            "planning -> activated is impossible: only the ledger commits, and activation "
            "follows an accepted commitment"
        )
    if current not in _MISSION_COMMIT_HOPS:
        raise ReservationTransitionError(f"{current.value} is not on the commit path")


def resolve_cancel_race(state: ReservationState | str) -> Rejection | None:
    """Deterministic answer when a cancellation races a commitment.

    Returns ``None`` when the cancellation is recorded (the commitment had not
    happened yet) and a rejection when the commitment already won. One function, used
    by both the concurrent and the sequential code path, so the two orderings cannot
    drift apart.
    """
    current = _state(state)
    if current in COMMITTED_STATES:
        return Rejection(
            RejectionCode.CANCEL_REFUSED_ALREADY_COMMITTED,
            f"reservation is {current.value}; the commitment won the race and the space stays held",
        )
    if current is ReservationState.UNRESOLVED:
        return Rejection(
            RejectionCode.CANCEL_REFUSED_ALREADY_COMMITTED,
            "reservation is unresolved; uncertainty is reconciled, not cancelled away",
        )
    if current in RELEASED_STATES:
        return Rejection(
            RejectionCode.MISSION_NOT_CANCELLABLE,
            f"reservation is already {current.value}",
        )
    return None


def first_protecting(
    candidates: Iterable[tuple[str, ReservationState | str]],
) -> Rejection | None:
    """The overlap a step-8 recheck found, if any, as a reasoned rejection.

    ``candidates`` is ``(resource_key, state)`` pairs already narrowed to genuine
    window overlap. ``unresolved`` is reported under its own code so the console can
    distinguish "someone holds it" from "nobody knows".
    """
    for key, state in sorted(candidates, key=lambda item: item[0]):
        current = _state(state)
        if current is ReservationState.UNRESOLVED:
            return Rejection(
                RejectionCode.UNRESOLVED_PROTECTION,
                f"{key} is held by an unresolved reservation; telemetry loss does not clear airspace",
                resource_key=key,
            )
        if current in COMMITTED_STATES:
            return Rejection(
                RejectionCode.COMMITTED_OVERLAP,
                f"{key} is already committed over an overlapping window",
                resource_key=key,
            )
    return None


def _state(value: ReservationState | str) -> ReservationState:
    return value if isinstance(value, ReservationState) else ReservationState(value)


__all__ = [
    "COMMITTED_STATES",
    "CommitAuthority",
    "MISSION_STATUS_ORDER",
    "PROTECTED_STATES",
    "RELEASED_STATES",
    "RESERVATION_TRANSITIONS",
    "UNRESOLVED_STATES",
    "UnresolvedHandling",
    "assert_mission_can_be_accepted",
    "assert_transition",
    "blocks_new_commitment",
    "first_protecting",
    "is_terminal",
    "may_be_released",
    "mission_commit_path",
    "protected_states",
    "releasable_states",
    "resolve_cancel_race",
]

MISSION_STATUS_ORDER: tuple[MissionStatus, ...] = _MISSION_COMMIT_HOPS
