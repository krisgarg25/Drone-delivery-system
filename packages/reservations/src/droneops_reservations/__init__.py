"""``droneops_reservations`` - commit state and atomic resource semantics, no database.

Two authorities meet here and neither is allowed to do the other's job:

* the **planner proposes**; this package decides what a commitment means, in what
  order resources are locked, which states still protect airspace, and what evidence
  a promotion requires. It never touches SQLAlchemy, asyncpg or
  ``droneops_persistence`` - ``.importlinter``'s ``domain-is-pure`` contract enforces
  that, which is why the SQL that executes the eleven steps lives in the sibling
  module ``droneops_reservations.ledger``.
* the **independent validator votes**. This package owns the promotion rule
  (:func:`promotion`) and the vocabulary it votes in (:class:`Verdict`); the checker
  itself is a different implementation, reached through the :class:`CommitValidator`
  port, so agreement between the two is evidence rather than a tautology.

The eleven steps of ``technical-build/01`` section 9 are :class:`CommitStep`, in
order, with numbers. A rejection is a value with a code and evidence
(:class:`Rejection`), never an exception message a reviewer has to parse.
"""

from __future__ import annotations

from .commit import (
    AllowRule,
    CommitValidator,
    DecisionTrace,
    FaultProgram,
    FinalSet,
    ProtectedHolding,
    TraceStepRecord,
    ValidationOutcome,
    deterministic_id,
    input_hash,
    ordered_holders,
    promotion,
    rejection_from_trace,
    summarise,
    trace_steps,
)
from .errors import (
    CommitRejected,
    IdentityMismatch,
    InternalFault,
    LedgerError,
    Rejection,
    RejectionCode,
    ReservationTransitionError,
    StepFault,
)
from .intake import (
    OPERABLE_AIRCRAFT,
    RELIABLE_LINK,
    AircraftCandidate,
    CandidatePlan,
    CommitCommand,
    EnergyVerdict,
    PreflightFailure,
    ProfileSample,
    Selection,
    Verdict,
    WorldView,
    eligible_aircraft,
    preflight,
    rank_candidates,
    reject_restricted,
)
from .legacy import (
    VALID_STATES,
    Reservation,
    can_overlap_committed,
    canonical_resource_key,
    validate_commit_set,
)
from .protocol import (
    STEP_SEQUENCE,
    CommitStep,
    ResourceClaim,
    find_self_conflict,
    identity_sort_key,
    in_lock_order,
    ledger_kind,
    lock_tokens,
    parse_stored_key,
    resource_claim_identity_order,
    stored_key_form,
    validate_lock_order,
    window_us,
)
from .states import (
    COMMITTED_STATES,
    MISSION_STATUS_ORDER,
    PROTECTED_STATES,
    RELEASED_STATES,
    RESERVATION_TRANSITIONS,
    UNRESOLVED_STATES,
    CommitAuthority,
    UnresolvedHandling,
    assert_mission_can_be_accepted,
    assert_transition,
    blocks_new_commitment,
    first_protecting,
    is_terminal,
    may_be_released,
    mission_commit_path,
    protected_states,
    releasable_states,
    resolve_cancel_race,
)

__all__ = [
    # ---- constants and vocabularies
    "COMMITTED_STATES",
    "MISSION_STATUS_ORDER",
    "OPERABLE_AIRCRAFT",
    "PROTECTED_STATES",
    "RELEASED_STATES",
    "RELIABLE_LINK",
    "RESERVATION_TRANSITIONS",
    "STEP_SEQUENCE",
    "UNRESOLVED_STATES",
    "VALID_STATES",
    # ---- the eleven steps, ordering and stored keys
    "CommitStep",
    "ResourceClaim",
    "find_self_conflict",
    "identity_sort_key",
    "in_lock_order",
    "ledger_kind",
    "lock_tokens",
    "parse_stored_key",
    "resource_claim_identity_order",
    "stored_key_form",
    "validate_lock_order",
    "window_us",
    # ---- state, uncertainty and authority (I3)
    "CommitAuthority",
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
    # ---- steps 3 to 6
    "AircraftCandidate",
    "CandidatePlan",
    "CommitCommand",
    "EnergyVerdict",
    "PreflightFailure",
    "ProfileSample",
    "Selection",
    "Verdict",
    "WorldView",
    "eligible_aircraft",
    "preflight",
    "rank_candidates",
    "reject_restricted",
    # ---- promotion, evidence and trace
    "AllowRule",
    "CommitValidator",
    "DecisionTrace",
    "FaultProgram",
    "FinalSet",
    "ProtectedHolding",
    "TraceStepRecord",
    "ValidationOutcome",
    "deterministic_id",
    "input_hash",
    "ordered_holders",
    "promotion",
    "rejection_from_trace",
    "summarise",
    "trace_steps",
    # ---- errors and rejection codes
    "CommitRejected",
    "IdentityMismatch",
    "InternalFault",
    "LedgerError",
    "Rejection",
    "RejectionCode",
    "ReservationTransitionError",
    "StepFault",
    # ---- Phase 0 surface, unchanged
    "Reservation",
    "can_overlap_committed",
    "canonical_resource_key",
    "validate_commit_set",
]
