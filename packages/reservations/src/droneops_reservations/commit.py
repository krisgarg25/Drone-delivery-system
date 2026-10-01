"""Promotion policy: what the ledger needs before it turns ``tentative`` into ``committed``.

This is the whole point of section 9. The ledger writes the reservation as
``tentative``, runs an **independent** validator over the *final* set (the new claims
plus every already-protected holder), and promotes only on the answer that permits it.
Three rules, and none of them is a preference:

``SAFE``
    promote.
``CONFLICT``
    refuse, unless this request explicitly named one allow rule *and* the validator
    independently reached the same rule, at the same version. Two parties naming the
    same exception is a decision; one party inventing an exception is a hole.
``UNPROVEN``
    always refuse, even with a named rule. A rule that permits a *known* overlap does
    not turn "the validator could not reason about this geometry" into clearance, and
    a validator that could be waved through by an exception when confused would fail
    open. This is the Phase 2 exit criterion "unless an explicit allow rule **and** the
    independent checker agree" read literally.

Why the validator is a port and not an import
    ``droneops_conflict_checker`` must never import the planner (conventions section 6)
    and the ledger must not import the checker's internals either, or the second opinion
    becomes a shared assumption. :class:`CommitValidator` is the seam: the ledger knows
    only ``validate(final_set) -> outcome``. The production adapter is
    ``droneops_reservations.ledger.validators.CheckerCommitValidator`` (real checker,
    ``check_set`` over the committed set); the deterministic one is
    ``IntervalCommitValidator``. Both are exercised by the same ledger code, so the
    "planner-approved, checker-convicted" proof is about the ledger, not about a stub.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable
from uuid import UUID

from .errors import CommitRejected, Rejection, RejectionCode, StepFault
from .intake import (
    CandidatePlan,
    CommitCommand,
    ProfileSample,
    Selection,
    Verdict,
    WorldView,
)
from .protocol import CommitStep, ResourceClaim
from .states import blocks_new_commitment, protects_space

_ID_NAMESPACE = uuid.UUID("2b6f7f0a-8f1e-5c3d-9a4f-0d1e2f3a4b5c")
"""Fixed namespace: derived identifiers are reproducible across processes and runs."""


def deterministic_id(*parts: str) -> uuid.UUID:
    """A stable identifier from the commitment identity, so a retry re-derives the same row.

    UUID5 over ``(token, part)`` rather than ``uuid4()`` is what makes "a duplicate
    request returns the same mission and the same commitment" true at the row level:
    the second attempt computes the same primary keys, and the database's own
    uniqueness turns a would-be second charge into a conflict the ledger reads back as
    the original answer.
    """
    return uuid.uuid5(_ID_NAMESPACE, "\x1f".join(parts))


@dataclass(frozen=True)
class ProtectedHolding:
    """One row that already holds part of a resource, as read at step 8.

    ``state`` is kept as a string because it is copied straight out of the database;
    :mod:`droneops_reservations.states` is the only place that decides what it means.
    The plan fields are the evidence the independent validator re-derives volumes
    from: a holder without them is a record the checker cannot evaluate, and that is an
    ``UNPROVEN``, not a free pass.
    """

    resource_key: str
    mission_id: UUID
    state: str
    valid_from: datetime
    valid_until: datetime
    points: tuple[ProfileSample, ...] = ()
    plan_version: str = ""
    policy_version: str = ""
    planner_verdict: str | None = None
    telemetry_last_fix: datetime | None = None

    @property
    def identity(self) -> str:
        """``<kind>:<identifier>`` - the physical resource this row holds."""
        parts = self.resource_key.split(":")
        return f"{parts[0]}:{parts[1]}"

    @property
    def protects(self) -> bool:
        return protects_space(self.state)

    @property
    def blocking(self) -> bool:
        return blocks_new_commitment(self.state)

    def as_row(self) -> tuple[str, str, str, str]:
        return (
            self.resource_key,
            self.state,
            self.mission_id.hex,
            self.valid_from.astimezone(timezone.utc).isoformat(),
        )


@dataclass(frozen=True)
class FinalSet:
    """Exactly what the validator is asked about: the set as it would be committed."""

    requested: tuple[ResourceClaim, ...]
    holders: tuple[ProtectedHolding, ...]
    policy_version: str
    config_release_sha: str
    candidate: CandidatePlan

    @property
    def protected_identities(self) -> tuple[str, ...]:
        return tuple(sorted({holder.resource_key.partition(":")[0] for holder in self.holders}))


@dataclass(frozen=True)
class AllowRule:
    """A named, versioned exception - and the only form of exception the ledger reads."""

    rule_id: str
    version: str

    def matches(self, other: AllowRule | None) -> bool:
        return other is not None and other == self


@dataclass(frozen=True)
class ValidationOutcome:
    """The validator's answer, with its own provenance attached.

    ``checker_name`` and ``checker_version`` are recorded because a verdict from an
    unidentified validator is not evidence; a later reviewer has to be able to tell
    which implementation voted.
    """

    verdict: Verdict
    reasons: tuple[str, ...] = ()
    grant: AllowRule | None = None
    policy_key: str = ""
    checker_name: str = "unknown"
    checker_version: str = "0"
    records_checked: int = 0
    pairs_checked: int = 0
    planner_disagreement: bool = False

    @property
    def summary(self) -> str:
        return (
            f"{self.checker_name}@{self.checker_version} {self.verdict.value} "
            f"over {self.records_checked} record(s)/{self.pairs_checked} pair(s)"
        )


@runtime_checkable
class CommitValidator(Protocol):
    """The independent vote. Read-only by construction: it gets a value, not a session."""

    name: str
    version: str

    def validate(self, final: FinalSet) -> ValidationOutcome:
        """Return SAFE, CONFLICT or UNPROVEN for the set as it would be committed."""
        ...


def promotion(outcome: ValidationOutcome, command: CommitCommand) -> Rejection | None:
    """Decide whether ``outcome`` permits a promotion. ``None`` means promote.

    Deliberately a pure function of two values, so the exact rule - including the
    allow-rule agreement requirement - is testable without a database, and so the
    ledger cannot promote by forgetting to call it.
    """
    if outcome.verdict is Verdict.SAFE and outcome.grant is None:
        return None
    named = command.named_allow_rule()
    if outcome.verdict is Verdict.UNPROVEN:
        return Rejection(
            RejectionCode.CHECKER_UNPROVEN,
            f"{outcome.summary}: " + "; ".join(outcome.reasons or ("no reason supplied",)),
        )
    if outcome.verdict is Verdict.SAFE:
        # A SAFE that arrived with a grant is an exception the validator applied on
        # its own initiative. The ledger requires it to be the one this request named.
        if named is not None and AllowRule(*named).matches(outcome.grant):
            return None
        return Rejection(
            RejectionCode.ALLOW_RULE_UNMATCHED,
            f"validator applied {outcome.grant} while this request named {named}",
        )
    if named is None:
        return Rejection(
            RejectionCode.CHECKER_CONFLICT,
            f"{outcome.summary}: " + "; ".join(outcome.reasons or ("no reason supplied",)),
        )
    claimed = AllowRule(named[0], named[1])
    if not claimed.matches(outcome.grant):
        return Rejection(
            RejectionCode.ALLOW_RULE_UNMATCHED,
            (
                f"request named {claimed.rule_id}@{claimed.version} but the validator "
                f"granted {outcome.grant.rule_id}@{outcome.grant.version}"
                if outcome.grant is not None
                else f"request named {claimed.rule_id}@{claimed.version} and no rule was granted"
            ),
        )
    return None


@dataclass(frozen=True)
class TraceStepRecord:
    """One step's answer, in the order the ledger took it."""

    step: CommitStep
    outcome: str
    detail: str = ""

    def as_row(self) -> tuple[int, str, str, str]:
        return (self.step.number, self.step.value, self.outcome, self.detail)


@dataclass(frozen=True)
class DecisionTrace:
    """Everything the ledger knows about one attempt, in the shape the rows want.

    ``decision_record`` and ``domain_event.payload_json`` are both written from this
    object, so the stored explanation and the emitted event cannot disagree - which is
    the difference between a ledger that can explain itself and one that can only
    recompute an answer it no longer has the inputs for.
    """

    command: CommitCommand
    input_hash: str
    reason_codes: tuple[RejectionCode, ...]
    steps: tuple[TraceStepRecord, ...]
    ranking: tuple[tuple[str, int, int, int], ...] = ()
    rejections: tuple[Rejection, ...] = ()
    outcome: ValidationOutcome | None = None
    lock_order: tuple[str, ...] = ()
    reservation_version: int | None = None
    committed: bool = False
    duplicate_of: uuid.UUID | None = None

    @property
    def primary_reason(self) -> RejectionCode:
        return self.reason_codes[0] if self.reason_codes else RejectionCode.COMMITTED

    def eligible_json(self) -> str:
        return json.dumps(
            {
                "config_release_sha": self.command.config_release_sha,
                "policy_version": self.command.policy_version,
                "snapshot_digest": self.command.world.snapshot_digest,
                "reservation_version": self.reservation_version,
                "candidates": [
                    {
                        "aircraft_id": aircraft_id,
                        "total_units": total,
                        "lateness_us": late,
                        "energy_margin_wh": margin,
                    }
                    for aircraft_id, total, late, margin in self.ranking
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    def rejected_json(self) -> str:
        return json.dumps(
            {
                "rejections": [
                    {
                        "code": rejection.code.value,
                        "detail": rejection.detail,
                        "resource_key": rejection.resource_key,
                        "candidate_id": rejection.candidate_id,
                        "aircraft_id": rejection.aircraft_id,
                    }
                    for rejection in self.rejections
                ],
                "steps": [
                    {"step": number, "name": name, "outcome": outcome, "detail": detail}
                    for number, name, outcome, detail in (step.as_row() for step in self.steps)
                ],
                "lock_order": list(self.lock_order),
                "checker": None
                if self.outcome is None
                else {
                    "name": self.outcome.checker_name,
                    "version": self.outcome.checker_version,
                    "verdict": self.outcome.verdict.value,
                    "reasons": list(self.outcome.reasons),
                    "policy_key": self.outcome.policy_key,
                    "grant": None
                    if self.outcome.grant is None
                    else [self.outcome.grant.rule_id, self.outcome.grant.version],
                    "records_checked": self.outcome.records_checked,
                    "pairs_checked": self.outcome.pairs_checked,
                    "planner_disagreement": self.outcome.planner_disagreement,
                },
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    def event_payload(self, reservation_ids: Sequence[uuid.UUID]) -> str:
        return json.dumps(
            {
                "commitment_token": self.command.commitment_token,
                "reason_codes": [code.value for code in self.reason_codes],
                "committed": self.committed,
                "input_hash": self.input_hash,
                "checker_verdict": None
                if self.outcome is None
                else self.outcome.verdict.value,
                "resource_keys": list(self.lock_order),
                "reservation_ids": [str(reservation_id) for reservation_id in reservation_ids],
            },
            sort_keys=True,
            separators=(",", ":"),
        )


def input_hash(command: CommitCommand, final: Mapping[str, str]) -> str:
    """The canonical hash of the accepted inputs - what a reviewer replays against.

    Canonical JSON with sorted keys and no whitespace, so two attempts that mean the
    same thing hash the same, and an attempt that changed one margin does not.
    """
    payload = {
        "tenant_id": str(command.tenant_id),
        "order_id": str(command.order_id),
        "commitment_token": command.commitment_token,
        "policy_version": command.policy_version,
        "config_release_sha": command.config_release_sha,
        "snapshot_digest": command.world.snapshot_digest,
        "world": dict(sorted(final.items())),
        "candidates": [
            [
                plan.candidate_id,
                plan.aircraft_id,
                plan.route_digest,
                [str(claim) for claim in plan.ordered_claims()],
            ]
            for plan in command.candidates
        ],
        "aircraft": [
            [
                aircraft.aircraft_id,
                aircraft.status.value,
                aircraft.link.value,
                aircraft.total_units,
                aircraft.energy.margin_wh,
            ]
            for aircraft in command.aircraft
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


@dataclass(frozen=True)
class FaultProgram:
    """Test-only seam that fails one named step, so each of the eleven can be proven atomic.

    Constructed explicitly by a test and passed into the ledger. There is no default,
    no environment variable and no global: production code paths cannot reach it, which
    is the same reason the repository has no command flag.
    """

    fail_at: CommitStep | None = None
    detail: str = "injected step fault"

    def fire(self, step: CommitStep) -> None:
        """Raise at the top of ``step`` if this program names it."""
        if step is self.fail_at:
            raise StepFault(step.value, self.detail)


def rejection_from_trace(trace: DecisionTrace) -> CommitRejected:
    """Turn a persisted rejection into the exception the caller sees."""
    return CommitRejected(
        trace.rejections[-1]
        if trace.rejections
        else Rejection(trace.primary_reason, "the ledger declined this proposal")
    )


def ordered_holders(holders: Sequence[ProtectedHolding]) -> tuple[ProtectedHolding, ...]:
    """Holders in (resource key, window, mission) order - the order step 8 locks and reads."""
    return tuple(
        sorted(
            (holder for holder in holders if holder.protects),
            key=lambda holder: (
                holder.resource_key,
                holder.valid_from,
                holder.valid_until,
                holder.mission_id.hex,
            ),
        )
    )


def summarise(world: WorldView, selection: Selection | None) -> Mapping[str, str]:
    """The frozen inputs reduced to the small mapping that rides in the trace."""
    base = {
        "policy_version": world.policy_version,
        "config_release_sha": world.config_release_sha,
        "snapshot_digest": world.snapshot_digest,
        "restricted_count": str(len(world.restricted_keys)),
        "unavailable_count": str(len(world.unavailable)),
    }
    if selection is None:
        return base
    return {**base, "candidate_id": selection.candidate.candidate_id,
            "aircraft_id": selection.aircraft.aircraft_id}


def trace_steps(records: Sequence[TraceStepRecord]) -> tuple[TraceStepRecord, ...]:
    """Steps in numbered order - the audit trail reads in transaction order."""
    return tuple(sorted(records, key=lambda record: record.step.number))


__all__ = [
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
]
