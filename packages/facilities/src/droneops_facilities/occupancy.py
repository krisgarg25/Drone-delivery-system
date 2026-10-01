"""What the facility scheduler may read, and what it may only offer.

The authority split from ``conventions.md`` sections 5 and 7 lives in this file:
:class:`CommittedOccupancy` is a read-only port the reservation ledger implements,
:class:`ProposalBook` records what this package has *offered*, and
:class:`AppendOnlyLedger` is a stand-in ledger for simulation runs that appends
and never edits. A committed claim is immutable here for the same reason it is
immutable in PostgreSQL: a correction is a superseding record.

The scheduler's view of a resource is the union of committed and offered
windows. Being conservative about its own proposals is what stops two requests
from being offered the same pad in one batch, which is the only way this package
could ever break I6 on its own.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from droneops_geom_core import Interval, merge

from .errors import CommitConflictError
from .model import ResourceClaim, TurnaroundPlan
from .resources import canonical_keys


class CommittedOccupancy(Protocol):
    """The committed world, as far as facilities is allowed to see it.

    Only the ledger may widen this view; nothing in this package writes through
    it, which is why the protocol has no mutating method to misremember calling.
    """

    def committed_windows(self, identity_token: str) -> tuple[Interval, ...]:
        """Windows already committed on one resource identity, ascending."""
        ...


@dataclass(frozen=True)
class CommitReceipt:
    """Proof that a proposal was handed over, in canonical lock order."""

    proposal_id: str
    keys: tuple[str, ...]
    recorded_at_us: int
    claim_count: int

    def as_metrics(self) -> dict[str, str | int]:
        return {
            "proposal_id": self.proposal_id,
            "claim_count": self.claim_count,
            "recorded_at_us": self.recorded_at_us,
            "lock_order": "|".join(self.keys),
        }


class AppendOnlyLedger(CommittedOccupancy):
    """A simulation ledger: append-only, conflict-rejecting, idempotent by id.

    ``commit`` re-checks every claim against what is already stored before
    appending. That is the boundary check that makes I6 observable in production
    rather than only in a post-run audit, so a scheduler bug cannot pass silently.
    """

    def __init__(self, initial: Iterable[ResourceClaim] = ()) -> None:
        self._claims: dict[str, list[ResourceClaim]] = {}
        self._receipts: dict[str, CommitReceipt] = {}
        for claim in sorted(initial, key=lambda item: (item.request_id, item.identity_token)):
            self.append(claim, recorded_at_us=claim.window.start)

    def append(self, claim: ResourceClaim, *, recorded_at_us: int) -> None:
        """Add one claim. Overlap on an identity is a hard failure.

        Raises:
            CommitConflictError: the claim overlaps an already-committed window
                on the same resource.
        """
        for held in self._claims.get(claim.identity_token, ()):
            if held.window.overlaps(claim.window):
                raise CommitConflictError(
                    f"{claim.identity_token}: {claim.request_id} overlaps committed "
                    f"{held.request_id} at {claim.window} vs {held.window}"
                )
        self._claims.setdefault(claim.identity_token, []).append(claim)

    def commit(self, plan: TurnaroundPlan, *, recorded_at_us: int) -> CommitReceipt:
        """Commit a proposal, or return the original receipt for a retry (I2).

        A duplicate ``proposal_id`` cannot create a second set of claims, so a
        retried handover is safe by construction rather than by convention.
        """
        existing = self._receipts.get(plan.proposal_id)
        if existing is not None:
            return existing
        snapshot = list(plan.claims)
        for claim in snapshot:
            self.append(claim, recorded_at_us=recorded_at_us)
        receipt = CommitReceipt(
            proposal_id=plan.proposal_id,
            keys=tuple(key.canonical for key in canonical_keys(plan.claims)),
            recorded_at_us=recorded_at_us,
            claim_count=len(snapshot),
        )
        self._receipts[plan.proposal_id] = receipt
        return receipt

    def committed_windows(self, identity_token: str) -> tuple[Interval, ...]:
        return tuple(
            sorted(
                (claim.window for claim in self._claims.get(identity_token, ())),
                key=lambda item: (item.start, item.end),
            )
        )

    def committed_claims(self, facility_id: str | None = None) -> tuple[ResourceClaim, ...]:
        """Every committed claim, or only those at one facility, ascending.

        Grouped by facility because that is the unit an outage and a capacity
        audit are measured against.
        """
        out: list[ResourceClaim] = []
        for identity in sorted(self._claims):
            out.extend(self._claims[identity])
        if facility_id is not None:
            out = [claim for claim in out if claim.facility_id == facility_id]
        return tuple(sorted(out, key=lambda item: (item.identity_token, item.window.start, item.request_id)))

    def claims_on(self, identity_token: str) -> tuple[ResourceClaim, ...]:
        """The claims held on one resource identity, ascending by window."""
        return tuple(
            sorted(
                self._claims.get(identity_token, ()),
                key=lambda item: (item.window.start, item.window.end, item.request_id),
            )
        )

    @property
    def committed_count(self) -> int:
        return sum(len(group) for group in self._claims.values())

    def supersede(self, plan: TurnaroundPlan, replacement: TurnaroundPlan, *, recorded_at_us: int) -> CommitReceipt:
        """Append a correction as a new record; the original stays.

        ``conventions.md`` section 5: committed records are append-only. The
        replacement must not overlap the original, so a correction that quietly
        re-uses held capacity is refused.
        """
        for original in replacement.claims:
            for held in plan.claims:
                if held.identity_token == original.identity_token and held.window.overlaps(original.window):
                    raise CommitConflictError(
                        f"superseding {held.request_id} cannot reuse {held.identity_token} "
                        f"while {original.window} is still on the record"
                    )
        return self.commit(replacement, recorded_at_us=recorded_at_us)


class ProposalBook:
    """What this scheduler has offered and the ledger has not yet taken.

    ``settle`` moves a proposal out of the book once the ledger confirms it;
    until then the offered windows keep blocking new offers. A stale offer is
    therefore conservative - it can delay work, never double-book it.
    """

    def __init__(self) -> None:
        self._plans: dict[str, TurnaroundPlan] = {}

    def offer(self, plan: TurnaroundPlan) -> bool:
        """Record an offer. ``False`` when the same proposal id was already made."""
        if plan.proposal_id in self._plans:
            return False
        self._plans[plan.proposal_id] = plan
        return True

    def settle(self, proposal_id: str) -> TurnaroundPlan | None:
        """Drop a settled offer, returning it. Unknown ids give ``None``."""
        return self._plans.pop(proposal_id, None)

    def pending(self) -> tuple[TurnaroundPlan, ...]:
        return tuple(self._plans[key] for key in sorted(self._plans))

    def pending_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._plans))

    def offered_windows(self, identity_token: str) -> tuple[Interval, ...]:
        out: list[Interval] = []
        for key in sorted(self._plans):
            plan = self._plans[key]
            out.extend(claim.window for claim in plan.claims if claim.identity_token == identity_token)
        return tuple(sorted(out, key=lambda item: (item.start, item.end)))


class Occupancy:
    """Committed windows plus this scheduler's own open offers.

    The single place the union is taken, so no caller can accidentally read only
    one half of it.
    """

    def __init__(self, committed: CommittedOccupancy, book: ProposalBook) -> None:
        self.committed = committed
        self.book = book

    def occupied(self, identity_token: str) -> tuple[Interval, ...]:
        """The merged busy windows on one identity, ascending and disjoint."""
        return merge(
            (
                *self.committed.committed_windows(identity_token),
                *self.book.offered_windows(identity_token),
            )
        )

    def busy_instant_count(self, identity_token: str) -> int:
        """How many claims sit on one identity. Unit resources cap at one."""
        return len(self.committed.committed_windows(identity_token)) + len(
            self.book.offered_windows(identity_token)
        )


__all__ = [
    "AppendOnlyLedger",
    "CommittedOccupancy",
    "Occupancy",
    "ProposalBook",
]
