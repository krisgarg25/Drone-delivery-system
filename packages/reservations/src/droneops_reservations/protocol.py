"""The eleven commit steps, the canonical lock order and the stored key form.

``docs/conventions.md`` section 5 and ``technical-build/01`` section 9 fix the
transaction as an ordered list. This module makes that list a *type* rather than a
comment: :class:`CommitStep` carries its number, :data:`STEP_SEQUENCE` is the only
legal order, and the ledger records the step it reached in every decision trace. A
step can therefore be proven present (and provably never reordered) by a test.

The stored resource key
-----------------------
``geom_core`` emits the ``technical-build/01`` section 6 tokens - ``volume:`` and
``charger:`` - while both ``droneops_contracts.ResourceKind`` and the live
``ck_reservation_kind_matches_key_prefix`` check constraint require the key prefix to
equal the ``resource_kind`` enum label, i.e. ``volume_cell:`` and ``charger_bay:``.
The database is the system of record, so the ledger writes the *stored* form and
bridges to and from geom-core's canonical form here, in one place, rather than
forking a second key grammar:

=========================  ====================  ==========================
kind                       geom-core canonical  ledger stored
=========================  ====================  ==========================
``CORRIDOR``               ``corridor:C-01``     ``corridor:C-01``
``VOLUME_CELL``            ``volume:C-01:1..2``  ``volume_cell:C-01:1..2``
``CHARGER_BAY``            ``charger:B-1:1..2``  ``charger_bay:B-1:1..2``
=========================  ====================  ==========================

The three other kinds are unaffected, and ``ResourceKey.parse`` already accepts both
spellings, so the bridge round-trips exactly.

Lock order
----------
Acquisition order comes from geom-core's single total order
(:func:`droneops_geom_core.sorted_resource_keys`, keyed on
``ResourceKey.lock_token``), never from the order a caller happened to submit, and
never from iterating a set. Two concurrent commits therefore lock in the same
sequence and cannot form a wait-for cycle.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import final

from droneops_geom_core.intervals import Interval
from droneops_geom_core.resources import ResourceKey, kind_from_token, resource_key
from droneops_geom_core.timebase import to_microseconds

from .errors import CommitRejected, Rejection, RejectionCode

STEP_SEQUENCE: tuple[str, ...] = (
    "validate duplicate identity",
    "freeze world snapshot",
    "rank eligible aircraft",
    "generate candidate routes",
    "reject restricted or unavailable candidates",
    "apply energy and recovery checks",
    "acquire row locks in canonical resource order",
    "recheck committed reservations",
    "insert mission, plan, reservations and decision record",
    "append the outbox event",
    "commit or roll back everything together",
)


@final
class CommitStep(StrEnum):
    """One numbered step of the section 9 transaction."""

    VALIDATE_DUPLICATE_IDENTITY = "validate_duplicate_identity"
    FREEZE_WORLD_SNAPSHOT = "freeze_world_snapshot"
    RANK_ELIGIBLE_AIRCRAFT = "rank_eligible_aircraft"
    GENERATE_CANDIDATE_ROUTES = "generate_candidate_routes"
    REJECT_RESTRICTED_CANDIDATES = "reject_restricted_candidates"
    APPLY_ENERGY_AND_RECOVERY_CHECKS = "apply_energy_and_recovery_checks"
    ACQUIRE_ROW_LOCKS = "acquire_row_locks_canonical_order"
    RECHECK_COMMITTED_RESERVATIONS = "recheck_committed_reservations"
    WRITE_OPERATIONAL_RECORDS = "insert_mission_plan_reservations_and_decision_record"
    APPEND_OUTBOX_EVENT = "append_outbox_event"
    COMMIT_OR_ROLLBACK = "commit_or_rollback_together"

    @property
    def number(self) -> int:
        """1-based position in ``STEP_SEQUENCE``; the order is the contract."""
        return _STEP_NUMBERS[self]

    @property
    def description(self) -> str:
        """The wording used by the architecture document, not a paraphrase."""
        return STEP_SEQUENCE[self.number - 1]

    def next(self) -> CommitStep:
        """The following step, or ``self`` after step 11 (the terminal step)."""
        order = list(CommitStep)
        index = order.index(self)
        return order[min(index + 1, len(order) - 1)]


_STEP_NUMBERS: dict[CommitStep, int] = {step: i + 1 for i, step in enumerate(CommitStep)}

assert len(CommitStep) == len(STEP_SEQUENCE), "the eleven steps are the sequence"


def stored_key_form(key: ResourceKey) -> str:
    """The exact string the ledger writes into ``reservation.resource_key``.

    Satisfies ``ck_reservation_kind_matches_key_prefix`` (prefix == ``resource_kind``
    label) and ``contracts.validate_resource_key``, and parses back through geom-core.
    """
    parts = [key.kind.name.lower(), key.identifier]
    if key.window is not None:
        parts.append(f"{key.window.start}..{key.window.end}")
    return ":".join(parts)


def parse_stored_key(stored: str) -> ResourceKey:
    """Read a stored key back into a geom-core key.

    Raises:
        droneops_geom_core.ResourceKeyError: the stored text is not canonical. There
        is no lenient parse: an unparsable key means the row is not a resource claim.
    """
    return ResourceKey.parse(stored)


def ledger_kind(stored: str) -> str:
    """The ``resource_kind`` enum label for a stored key (its first field)."""
    kind, _, rest = stored.partition(":")
    if not rest:
        raise ValueError(f"stored resource key {stored!r} has no qualifier")
    return kind


@dataclass(frozen=True)
class ResourceClaim:
    """One requested occupancy of one resource over one window.

    The window is always present and always in ``Interval`` form (half-open integer
    microseconds), because ``datetime`` arithmetic is exactly where an off-by-one
    becomes a collision. The key may be a bare corridor key - a corridor's temporal
    occupancy is expressed by its cells (section 6), while the corridor row itself is
    still what the ledger locks and what the exclusion constraint compares.
    """

    key: ResourceKey
    window: Interval

    @property
    def stored_key(self) -> str:
        return stored_key_form(self.key)

    @property
    def identity(self) -> str:
        """``<kind>:<identifier>`` - the physical thing, ignoring the window."""
        return f"{self.key.kind.name.lower()}:{self.key.identifier}"

    @property
    def valid_from(self) -> datetime:
        return self.window.as_datetimes()[0]

    @property
    def valid_until(self) -> datetime:
        return self.window.as_datetimes()[1]

    def conflicts_with(self, other: ResourceClaim) -> bool:
        """Same physical resource and non-empty window intersection.

        Adjacency is not overlap: ``[0, 5)`` and ``[5, 9)`` touch and are allowed,
        which is the half-open rule geom-core states once and this ledger relies on.
        """
        return self.identity == other.identity and self.window.overlaps(other.window)

    @classmethod
    def of(cls, kind: str, identifier: str, start: datetime, end: datetime) -> ResourceClaim:
        """Build a claim from datetimes; both ends must be UTC-aware."""
        window = Interval.from_datetimes(start, end)
        return cls(resource_key(kind, identifier, None if kind == "corridor" else window), window)

    def __str__(self) -> str:
        return f"{self.stored_key}@{self.window.start}..{self.window.end}"


def _claims_by_key(claims: Iterable[ResourceClaim]) -> dict[tuple[str, int, int], ResourceClaim]:
    """Claims keyed by (lock token, window): the deduplication identity of one claim.

    The window is part of the key because a *corridor* key carries none: two corridor
    claims over different minutes would otherwise collapse into one row and the second
    window would silently never be reserved.
    """
    unique: dict[tuple[str, int, int], ResourceClaim] = {}
    for claim in claims:
        unique.setdefault((claim.key.lock_token(), claim.window.start, claim.window.end), claim)
    return unique


def in_lock_order(claims: Iterable[ResourceClaim]) -> tuple[ResourceClaim, ...]:
    """Claims in canonical acquisition order, exact duplicates removed.

    Deduplication is of *identical* claims only (same resource, same window): two claims
    over the same resource with different windows are kept, and
    :func:`find_self_conflict` reports that pair as a self-conflict rather than merging
    it, because merging would under-reserve.
    """
    by_key = _claims_by_key(claims)
    return tuple(
        sorted(by_key.values(), key=lambda claim: (claim.key.lock_token(), claim.window.start, claim.window.end))
    )


def lock_tokens(claims: Iterable[ResourceClaim]) -> tuple[str, ...]:
    """Stored keys in acquisition order - what the transaction locks, in order.

    Corridor rows collapse to one entry (a bare key has no window), so the ledger
    locks one physical row per resource even when a plan uses it over several
    windows. That is what makes the lock set finite and the order total.
    """
    tokens: dict[str, None] = {}
    for claim in in_lock_order(claims):
        tokens.setdefault(claim.identity, None)
    return tuple(tokens)


def find_self_conflict(claims: Iterable[ResourceClaim]) -> Rejection | None:
    """A candidate that overlaps itself is incoherent, and no locking fixes that."""
    ordered = in_lock_order(claims)
    for index, left in enumerate(ordered):
        for right in ordered[index + 1 :]:
            if left.conflicts_with(right):
                return Rejection(
                    RejectionCode.CANDIDATE_SELF_OVERLAP,
                    f"candidate claims {left.identity} over {left.window} and {right.window}",
                    resource_key=left.stored_key,
                )
    return None


def validate_lock_order(claims: Iterable[ResourceClaim]) -> tuple[ResourceClaim, ...]:
    """Non-empty, canonical, self-consistent - or a reasoned rejection.

    Returns the ordered claims so a caller cannot lock an unsorted copy. A caller
    that wants the rejection as a value rather than an exception catches
    :class:`~droneops_reservations.errors.CommitRejected`.
    """
    materialised = tuple(claims)
    if not materialised:
        raise CommitRejected(
            Rejection(
                RejectionCode.LOCK_ORDER_INVALID,
                "a commit must claim at least one resource",
            )
        )
    ordered = in_lock_order(materialised)
    self_conflict = find_self_conflict(materialised)
    if self_conflict is not None:
        raise CommitRejected(self_conflict)
    return ordered


def window_us(moment: datetime) -> int:
    """UTC datetime to integer microseconds, the only time unit the ledger uses."""
    return int(to_microseconds(moment))


def identity_sort_key(identity: str) -> tuple[int, str]:
    """The canonical order of a bare resource identity (``<kind>:<identifier>``).

    The same kind rank and identifier comparison as :meth:`ResourceKey.sort_key` with the
    window term dropped, because an identity has none. A cancellation holds only stored
    keys and still locks in this order, so the ledger has exactly one acquisition
    sequence - two orders is what a deadlock cycle needs.
    """
    kind, _, identifier = identity.partition(":")
    if not identifier:
        raise ValueError(f"resource identity {identity!r} has no identifier")
    return (kind_from_token(kind).lock_rank, identifier)


def resource_claim_identity_order(identities: Iterable[str]) -> tuple[str, ...]:
    """Identities in canonical acquisition order, duplicates removed.

    ``dict.fromkeys`` rather than a ``set``, because conventions section 4 forbids
    producing order out of set iteration: the result must be a deduplicated *sequence*.
    """
    return tuple(sorted(dict.fromkeys(identities), key=identity_sort_key))


__all__ = [
    "STEP_SEQUENCE",
    "CommitStep",
    "ResourceClaim",
    "identity_sort_key",
    "in_lock_order",
    "ledger_kind",
    "lock_tokens",
    "find_self_conflict",
    "parse_stored_key",
    "resource_claim_identity_order",
    "stored_key_form",
    "validate_lock_order",
    "window_us",
]
