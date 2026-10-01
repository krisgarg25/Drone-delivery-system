"""Ledger errors and the rejection vocabulary.

Two different things can end a commit attempt and they must never be confused:

* a **rejection** - the ledger decided, from evidence, that it will not commit this
  plan. It is a first-class result (conventions section 11), it persists a decision
  record, and it leaves the ledger otherwise untouched.
* a **fault** - the transaction could not complete (driver error, constraint the
  ledger did not expect, an injected step failure). Everything rolls back and
  *nothing* is persisted, because a half-written explanation is worse than none.

``docs/conventions.md`` section 10 forbids swallowing an exception around a commit,
so every fault here is raised, never logged-and-ignored, and ``InternalFault``
always keeps the driver exception as ``__cause__``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import final


class RejectionCode(StrEnum):
    """Every reason the ledger can refuse, with a stable wire value.

    Phase 4's console explains a decision by reading these codes back out of
    ``decision_record.reason_codes``; a code that changes value silently rewrites
    history, so the set is closed and each member carries its own explanation.
    """

    DUPLICATE_REQUEST_RESOLVED = "ledger.duplicate_request_resolved"
    IDENTITY_MISMATCH = "ledger.identity_mismatch"
    SNAPSHOT_NOT_FROZEN = "ledger.snapshot_not_frozen"
    NO_ELIGIBLE_AIRCRAFT = "ledger.no_eligible_aircraft"
    AIRCRAFT_NOT_OPERABLE = "ledger.aircraft_not_operable"
    NO_CANDIDATE_PLANS = "ledger.no_candidate_plans"
    CANDIDATE_RESTRICTED = "ledger.candidate_restricted"
    RESOURCE_UNAVAILABLE = "ledger.resource_unavailable"
    CANDIDATE_SELF_OVERLAP = "ledger.candidate_self_overlap"
    ENERGY_MARGIN_BELOW_RESERVE = "ledger.energy_margin_below_reserve"
    RECOVERY_NOT_FEASIBLE = "ledger.recovery_not_feasible"
    LOCK_ORDER_INVALID = "ledger.lock_order_invalid"
    COMMITTED_OVERLAP = "ledger.committed_overlap"
    UNRESOLVED_PROTECTION = "ledger.unresolved_protection"
    STALE_RESERVATION_VERSION = "ledger.stale_reservation_version"
    CHECKER_CONFLICT = "checker.conflict"
    CHECKER_UNPROVEN = "checker.unproven"
    ALLOW_RULE_UNMATCHED = "checker.allow_rule_unmatched"
    COMMITTED = "ledger.committed"
    CANCELLED_BY_REQUEST = "ledger.cancelled_by_request"
    CANCEL_REFUSED_ALREADY_COMMITTED = "ledger.cancel_refused_already_committed"
    MISSION_NOT_CANCELLABLE = "ledger.mission_not_cancellable"
    CANCELLED_BEFORE_COMMIT = "ledger.cancelled_before_commit"

    @property
    def explanation(self) -> str:
        """The human sentence Phase 4 shows beside the code."""
        return _EXPLANATIONS[self]


_EXPLANATIONS: dict[RejectionCode, str] = {
    RejectionCode.DUPLICATE_REQUEST_RESOLVED: (
        "this request was already committed; the original mission and commitment were returned"
    ),
    RejectionCode.IDENTITY_MISMATCH: (
        "the idempotency key is already bound to a different tenant, order or resource set"
    ),
    RejectionCode.SNAPSHOT_NOT_FROZEN: (
        "the inputs of this plan are not a frozen, hashable world snapshot"
    ),
    RejectionCode.NO_ELIGIBLE_AIRCRAFT: "no aircraft survived the eligibility filter",
    RejectionCode.AIRCRAFT_NOT_OPERABLE: (
        "the aircraft is offline, in maintenance or its state is uncertain"
    ),
    RejectionCode.NO_CANDIDATE_PLANS: "the proposal carried no candidate route at all",
    RejectionCode.CANDIDATE_RESTRICTED: "a candidate crosses restricted airspace",
    RejectionCode.RESOURCE_UNAVAILABLE: "a candidate needs a resource that is not available",
    RejectionCode.CANDIDATE_SELF_OVERLAP: (
        "a candidate claims the same resource twice over overlapping windows"
    ),
    RejectionCode.ENERGY_MARGIN_BELOW_RESERVE: (
        "the energy margin is below the versioned reserve floor"
    ),
    RejectionCode.RECOVERY_NOT_FEASIBLE: (
        "recovery with reserve is not feasible for the whole mission"
    ),
    RejectionCode.LOCK_ORDER_INVALID: (
        "the resource set is empty or cannot be put in canonical lock order"
    ),
    RejectionCode.COMMITTED_OVERLAP: (
        "another committed reservation already holds part of this window"
    ),
    RejectionCode.UNRESOLVED_PROTECTION: (
        "an unresolved reservation still protects this window; uncertainty is not empty airspace"
    ),
    RejectionCode.STALE_RESERVATION_VERSION: (
        "the ledger moved on since this plan was priced; recompute instead of commit"
    ),
    RejectionCode.CHECKER_CONFLICT: "the independent checker proved a contact in the final set",
    RejectionCode.CHECKER_UNPROVEN: (
        "the independent checker could not prove the final set; unknown never releases airspace"
    ),
    RejectionCode.ALLOW_RULE_UNMATCHED: (
        "the exception was not named, versioned and accepted by the checker as well"
    ),
    RejectionCode.COMMITTED: "the whole resource set was committed atomically",
    RejectionCode.CANCELLED_BY_REQUEST: "the cancellation was recorded before any commitment",
    RejectionCode.CANCEL_REFUSED_ALREADY_COMMITTED: (
        "the commitment won the race; the airspace stays protected and the cancellation was declined"
    ),
    RejectionCode.MISSION_NOT_CANCELLABLE: "this mission has no reservation left to cancel",
    RejectionCode.CANCELLED_BEFORE_COMMIT: (
        "a cancellation reached the ledger before this commit; the mission is cancelled and "
        "gets no airspace"
    ),
}


@dataclass(frozen=True)
class Rejection:
    """A reasoned refusal, with the evidence that produced it."""

    code: RejectionCode
    detail: str
    resource_key: str | None = None
    candidate_id: str | None = None
    aircraft_id: str | None = None

    @property
    def reason_codes(self) -> tuple[RejectionCode, ...]:
        return (self.code,)

    def explain(self) -> str:
        return f"{self.code.value}: {self.detail}"


class LedgerError(RuntimeError):
    """Base class for every ledger failure that is not a reasoned rejection."""


class CommitRejected(LedgerError):
    """Raised at a transaction boundary when a rejection must abort the commit.

    Carrying the ``Rejection`` in the exception (rather than raising ``ValueError``
    with a formatted string) is what lets the caller persist the reason without
    parsing prose.
    """

    def __init__(self, rejection: Rejection) -> None:
        super().__init__(rejection.explain())
        self.rejection = rejection


class IdentityMismatch(LedgerError):
    """The same idempotency key presented two different resource sets."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


@final
class StepFault(LedgerError):
    """A fault injected at one of the eleven steps, to prove the rollback is total.

    Only ever constructed by a test through an explicit
    :class:`~droneops_reservations.commit.FaultProgram`. There is no environment
    variable, no global and no API route that can reach it, so a production commit
    cannot be made to fail on demand.
    """

    def __init__(self, step_name: str, detail: str) -> None:
        super().__init__(f"injected fault at step {step_name}: {detail}")
        self.step_name = step_name
        self.detail = detail


class InternalFault(LedgerError):
    """A database or driver failure the ledger did not reason about.

    Always paired with the original exception (``raise InternalFault(...) from exc``)
    and always rolls the whole transaction back. It is never reported as a rejection,
    because a rejection is a safety statement and a connection error is not one.
    """

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class ReservationTransitionError(LedgerError):
    """A reservation state change the state machine does not allow."""


__all__ = [
    "CommitRejected",
    "IdentityMismatch",
    "InternalFault",
    "LedgerError",
    "Rejection",
    "RejectionCode",
    "ReservationTransitionError",
    "StepFault",
]
