"""``ReservationCommitLedger``: the eleven steps of section 9, as one transaction.

The shape of the code is the shape of the specification. Every step is numbered in the
source, every step fires the (test-only) fault program before it does any work, and the
whole thing runs inside one database transaction:

* steps 1 to 8 read and lock, and can produce a **reasoned rejection** - no operational
  row has been written yet at any of those points;
* step 9 writes mission, plan version and *tentative* reservations inside a savepoint;
* the independent validator is then run over the final set as the database holds it,
  which is only possible because the tentative rows exist;
* ``SAFE`` promotes and releases the savepoint; ``CONFLICT`` / ``UNPROVEN`` roll the
  savepoint back - the operational rows vanish - and the explanation is written instead;
* step 10 appends the event and its outbox row in the same transaction;
* step 11 is the commit. There is no path from a rejection to a commitment, and no path
  from a fault to a decision record.

Two facts about why the pieces sit where they do:

Row locks in canonical order (step 7)
    ``resource_catalog`` holds one row per physical resource and the ledger locks them in
    ``geom-core``'s total order. Two commits wanting the same corridor queue on the same
    rows in the same direction, which is what a deadlock cycle cannot be built from. The
    database's ``no_committed_overlap`` exclusion remains the last guard - a write that
    bypasses the ledger is still refused by Postgres - but it is not the first guard, and
    it is not sufficient for windowed keys, whose stored strings differ per window.

The checker votes and its vote is not a preference
    Promotion happens only through :func:`droneops_reservations.commit.promotion`. A
    planner ``SAFE`` next to a checker ``CONFLICT`` becomes a ``CHECKER_CONFLICT``
    rejection with both verdicts stored, because that disagreement is the release blocker
    conventions section 6 names.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timezone
from typing import Final
from uuid import UUID

from droneops_geom_core.intervals import Interval
from droneops_platform.ports import TenantContext

from droneops_reservations import preflight
from droneops_reservations.commit import (
    DecisionTrace,
    FaultProgram,
    FinalSet,
    ProtectedHolding,
    TraceStepRecord,
    ValidationOutcome,
    deterministic_id,
    input_hash,
    promotion,
    summarise,
)
from droneops_reservations.errors import InternalFault, Rejection, RejectionCode
from droneops_reservations.intake import CommitCommand, PreflightFailure, Selection, WorldView
from droneops_reservations.protocol import (
    CommitStep,
    ResourceClaim,
    identity_sort_key,
    in_lock_order,
    resource_claim_identity_order,
)
from droneops_reservations.states import (
    assert_mission_can_be_accepted,
    assert_transition,
    mission_commit_path,
    protected_states,
    resolve_cancel_race,
)

from .store import LedgerStore, OrderRow, ReservationRow, Transaction, WorldRead
from .validators import PlanRecordPayload, identity_of

_EVENT_BY_CODE: Final[dict[RejectionCode, str]] = {
    RejectionCode.CHECKER_CONFLICT: "conflict.detected",
    RejectionCode.CHECKER_UNPROVEN: "checker.verdictRecorded",
    RejectionCode.ALLOW_RULE_UNMATCHED: "checker.verdictRecorded",
    RejectionCode.CANCELLED_BY_REQUEST: "reservation.cancelled",
    RejectionCode.UNRESOLVED_PROTECTION: "reservation.unresolved",
    RejectionCode.COMMITTED: "reservation.committed",
}
_DECLINED_EVENT: Final[str] = "proposal.declined"
"""Anything not otherwise mapped is recorded as a declined proposal - the catalogue's
own word for "the ledger said no", and there is no silent no-event path."""

_CANCEL_RACE_STATES: Final[tuple[str, ...]] = ("tentative",)
"""Only a row that was never committed is cancellable by a race.

``committed`` and ``activated`` are absent because the commitment won and because
priority may never delete an airborne commitment; ``unresolved`` is absent because
uncertainty is reconciled, not cancelled away (I3).
"""

_TELEMETRY_STATES: Final[tuple[str, ...]] = ("committed", "activated")
"""The states telemetry loss may move to ``unresolved``."""

_MISSION_CANCELLABLE_STATES: Final[tuple[str, ...]] = (
    "draft",
    "eligible",
    "planning",
    "reserved_pending_commit",
    "accepted",
    "failed",
)
"""Mission states a cancellation may record itself against.

``activated`` and ``in_progress`` are absent: an airborne mission's records are corrected
by a superseding record, never by a cancellation that rewrites what flew (conventions
section 5, section 9). ``failed`` is present because a mission that never obtained
airspace is exactly the thing an operator means when they cancel.
"""


def utc_now() -> datetime:
    """The only wall clock the ledger touches, and only for event timestamps."""
    return datetime.now(timezone.utc)


class Declined(Exception):
    """Internal carrier for a reasoned refusal: the rejection, and what it had touched.

    Separate from :class:`droneops_reservations.errors.StepFault` and from
    :class:`droneops_reservations.errors.InternalFault` precisely because the caller's
    response differs: a ``Declined`` persists its explanation and commits; a fault rolls
    the whole transaction back and persists nothing.
    """

    def __init__(
        self,
        rejection: Rejection,
        summary: Mapping[str, str],
        vote: ValidationOutcome | None = None,
    ) -> None:
        super().__init__(rejection.explain())
        self.rejection = rejection
        self.summary = summary
        self.vote = vote


class CommitReceipt:
    """The ledger's whole answer to one commit attempt.

    ``duplicate`` distinguishes "this request was already committed" from "this request
    just committed" while both carry the same identifiers - the difference invariant I2
    is about.
    """

    __slots__ = (
        "checker",
        "code",
        "commitment_token",
        "committed",
        "decision_record_id",
        "detail",
        "duplicate",
        "event_id",
        "lock_order",
        "mission_id",
        "plan_version_id",
        "reservation_ids",
        "reservation_version",
    )

    def __init__(
        self,
        *,
        mission_id: UUID,
        plan_version_id: UUID | None,
        commitment_token: str,
        reservation_ids: tuple[UUID, ...],
        decision_record_id: UUID,
        event_id: UUID,
        committed: bool,
        duplicate: bool,
        code: RejectionCode,
        detail: str,
        lock_order: tuple[str, ...],
        checker: ValidationOutcome | None,
        reservation_version: int,
    ) -> None:
        self.mission_id = mission_id
        self.plan_version_id = plan_version_id
        self.commitment_token = commitment_token
        self.reservation_ids = reservation_ids
        self.decision_record_id = decision_record_id
        self.event_id = event_id
        self.committed = committed
        self.duplicate = duplicate
        self.code = code
        self.detail = detail
        self.lock_order = lock_order
        self.checker = checker
        self.reservation_version = reservation_version

    @property
    def explained(self) -> str:
        return f"{self.code.value}: {self.detail}"

    def __repr__(self) -> str:
        return (
            f"CommitReceipt(mission={self.mission_id}, committed={self.committed}, "
            f"duplicate={self.duplicate}, code={self.code.value}, "
            f"reservations={len(self.reservation_ids)})"
        )


class CancelReceipt:
    """What a cancellation did, and equally important: what it refused to do."""

    __slots__ = ("code", "declined", "decision_record_id", "detail", "event_id", "mission_id", "transitioned")

    def __init__(
        self,
        *,
        mission_id: UUID,
        transitioned: int,
        declined: int,
        code: RejectionCode,
        detail: str,
        decision_record_id: UUID,
        event_id: UUID,
    ) -> None:
        self.mission_id = mission_id
        self.transitioned = transitioned
        self.declined = declined
        self.code = code
        self.detail = detail
        self.decision_record_id = decision_record_id
        self.event_id = event_id

    @property
    def cancelled(self) -> bool:
        return self.transitioned > 0

    @property
    def commit_won(self) -> bool:
        return self.code is RejectionCode.CANCEL_REFUSED_ALREADY_COMMITTED


#: Mission states the ledger may still walk forward to ``reserved_pending_commit``. Anything
#: past this point on the commit path is a mission that already holds airspace, and the
#: ledger refuses it with a reason instead of re-committing it (conventions section 9).
_REOPENABLE = frozenset({"draft", "eligible", "planning", "reserved_pending_commit"})


class ReservationCommitLedger:
    """The only writer of committed airspace in the system."""

    def __init__(
        self,
        store: LedgerStore,
        validator: Callable[[FinalSet], ValidationOutcome],
        *,
        clock: Callable[[], datetime] = utc_now,
        fault: FaultProgram | None = None,
    ) -> None:
        self._store = store
        self._vote = validator
        self._clock = clock
        self._fault = fault if fault is not None else FaultProgram()

    # ------------------------------------------------------------------ the transaction
    async def commit(self, context: TenantContext, command: CommitCommand) -> CommitReceipt:
        """Steps 1 to 11. One transaction, all rows or no rows.

        A reasoned refusal returns a receipt with ``committed=False`` and the code that
        explains it; a fault propagates and the transaction rolls back. The two are never
        conflated - that distinction is the deliverable.
        """
        if context.tenant_id != command.tenant_id:
            raise InternalFault(
                f"tenant context {context.tenant_id} does not match command {command.tenant_id}"
            )
        async with self._store.transaction(command.tenant_id) as tx:
            try:
                return await self._commit_in_transaction(tx, command)
            except Declined as refusal:
                await self._persist_rejection(tx, command, refusal)
                return await self._declined_receipt(tx, command, refusal)

    async def cancel(self, context: TenantContext, mission_id: UUID, reason_code: str) -> CancelReceipt:
        """A cancellation racing a commitment: the commitment wins, else it is recorded.

        The same canonical resource locks are taken first, so a cancel that overlaps an
        in-flight commit queues behind it rather than interleaving with it, and then acts
        on whichever state the rows are actually in.
        """
        command = _trace_command(context, mission_id, f"cancel:{mission_id}")
        async with self._store.transaction(context.tenant_id) as tx:
            # The same advisory key a commit takes at step 1, so a cancellation and the
            # commitment it races cannot interleave: whichever acquires first decides what
            # the other one sees.
            await self._store.lock_identity(tx, f"mission:{mission_id}")
            if await self._store.mission_status(tx, mission_id) is None:
                return CancelReceipt(
                    mission_id=mission_id,
                    transitioned=0,
                    declined=0,
                    code=RejectionCode.MISSION_NOT_CANCELLABLE,
                    detail=f"mission {mission_id} does not exist for this tenant",
                    decision_record_id=_NIL_ID,
                    event_id=_NIL_ID,
                )
            rows = await self._store.mission_rows(tx, mission_id)
            if rows:
                await self._store.lock_resources(tx, _lock_sequence(row.resource_key for row in rows))
                rows = await self._store.mission_rows(tx, mission_id)
            declined = [race for row in rows if (race := resolve_cancel_race(row.state)) is not None]
            cancellable = any(resolve_cancel_race(row.state) is None for row in rows)
            transitioned = (
                await self._store.cancel(tx, mission_id, reason_code, _CANCEL_RACE_STATES) if cancellable else 0
            )
            if declined:
                code = RejectionCode.CANCEL_REFUSED_ALREADY_COMMITTED
                detail = declined[0].detail
            elif transitioned or not rows:
                moved = await self._store.cancel_mission(tx, mission_id, _MISSION_CANCELLABLE_STATES)
                code = RejectionCode.CANCELLED_BY_REQUEST
                detail = (
                    f"cancelled {transitioned} reservation(s) and the mission ({moved} row(s) moved)"
                    if transitioned
                    else "no commitment existed yet; the cancellation is recorded on the mission"
                )
            else:
                code = RejectionCode.MISSION_NOT_CANCELLABLE
                detail = "nothing left to cancel"
            trace = DecisionTrace(
                command=command,
                input_hash=input_hash(
                    command, {"reason": reason_code, "states": ",".join(sorted(row.state for row in rows))}
                ),
                reason_codes=(code,),
                steps=(TraceStepRecord(CommitStep.COMMIT_OR_ROLLBACK, "cancel", code.value),),
                rejections=tuple(declined),
                lock_order=_lock_sequence(row.resource_key for row in rows),
            )
            decision_id = deterministic_id(str(mission_id), "decision", code.value)
            event_id = deterministic_id(str(mission_id), "event", code.value)
            await self._write_decision(tx, command, mission_id, None, decision_id, trace)
            await self._append_event(tx, command, mission_id, event_id, trace, code, ())
            return CancelReceipt(
                mission_id=mission_id,
                transitioned=transitioned,
                declined=len(declined),
                code=code,
                detail=detail,
                decision_record_id=decision_id,
                event_id=event_id,
            )

    async def mark_unresolved(self, context: TenantContext, mission_id: UUID, reason: str) -> int:
        """Telemetry loss: the reservation becomes uncertain and stays protected (I3)."""
        command = _trace_command(context, mission_id, f"unresolved:{mission_id}")
        async with self._store.transaction(context.tenant_id) as tx:
            rows = await self._store.mission_rows(tx, mission_id)
            if not rows:
                return 0
            await self._store.lock_resources(tx, _lock_sequence(row.resource_key for row in rows))
            rows = await self._store.mission_rows(tx, mission_id)
            moving = [row for row in rows if row.state in _TELEMETRY_STATES]
            for row in moving:
                assert_transition(row.state, "unresolved")
            if not moving:
                return 0
            ids = tuple(row.reservation_id for row in moving)
            changed = await self._store.mark_unresolved(tx, ids, _TELEMETRY_STATES)
            trace = DecisionTrace(
                command=command,
                input_hash=input_hash(command, {"reason": reason}),
                reason_codes=(RejectionCode.UNRESOLVED_PROTECTION,),
                steps=(TraceStepRecord(CommitStep.RECHECK_COMMITTED_RESERVATIONS, "unresolved", reason),),
                rejections=(Rejection(RejectionCode.UNRESOLVED_PROTECTION, reason),),
                lock_order=_lock_sequence(row.resource_key for row in rows),
            )
            decision_id = deterministic_id(str(mission_id), "decision", "unresolved")
            event_id = deterministic_id(str(mission_id), "event", "unresolved")
            await self._write_decision(tx, command, mission_id, None, decision_id, trace)
            await self._append_event(
                tx, command, mission_id, event_id, trace, RejectionCode.UNRESOLVED_PROTECTION, ids
            )
            return changed

    async def committed_overlapping(
        self, context: TenantContext, resource_key: str, valid_from: datetime, valid_until: datetime
    ) -> tuple[ReservationRow, ...]:
        """What already holds this resource - including the rows no index would report."""
        async with self._store.transaction(context.tenant_id) as tx:
            return await self._store.committed_overlapping(tx, resource_key, valid_from, valid_until)

    async def decisions(self, context: TenantContext, mission_id: UUID) -> tuple[str, ...]:
        """The reason codes Phase 4 will explain this mission from, newest last."""
        async with self._store.transaction(context.tenant_id) as tx:
            return tuple(
                code for decision in await self._store.decisions(tx, mission_id) for code in decision.reason_codes
            )

    async def policy_violations(self, context: TenantContext) -> tuple[tuple[str, str], ...]:
        """The exit criterion read back from the database: overlapping protected pairs."""
        async with self._store.transaction(context.tenant_id) as tx:
            rows = await self._store.policy_violations(tx, protected_states())
            return tuple((str(row["left_key"]), str(row["right_key"])) for row in rows)

    # ------------------------------------------------------------------ steps 1 to 11
    async def _commit_in_transaction(self, tx: Transaction, command: CommitCommand) -> CommitReceipt:
        steps: list[TraceStepRecord] = []

        # ---- step 1: validate duplicate request identity
        self._fault.fire(CommitStep.VALIDATE_DUPLICATE_IDENTITY)
        await self._store.lock_identity(tx, command.commitment_token)
        order: OrderRow | None = await self._store.find_order(tx, command.idempotency_key)
        if order is None:
            raise Declined(
                Rejection(
                    RejectionCode.IDENTITY_MISMATCH,
                    f"no order for idempotency key {command.idempotency_key!r} in this tenant",
                ),
                summarise(command.world, None),
            )
        if order.order_id != command.order_id:
            raise Declined(
                Rejection(
                    RejectionCode.IDENTITY_MISMATCH,
                    f"idempotency key {command.idempotency_key!r} belongs to order {order.order_id}, "
                    f"not {command.order_id}",
                ),
                summarise(command.world, None),
            )
        # One mission per order is the schema's own idempotency guarantee (``uq
        # tenant_id_order_id``), so the mission identity is derived from the order, never
        # from the token: a second token replayed against another order cannot manufacture
        # a second row for the first one.
        derived_mission_id = deterministic_id(str(order.order_id), "mission")
        await self._store.lock_identity(tx, f"mission:{derived_mission_id}")
        mission = await self._store.find_mission_by_order(tx, order.order_id)
        if mission is not None and mission.status == "cancelled":
            # Ordering (b) of the cancel/commit race: the cancellation got here first, it
            # is recorded, and this commit gets no airspace.
            raise Declined(
                Rejection(
                    RejectionCode.CANCELLED_BEFORE_COMMIT,
                    f"mission {mission.mission_id} was cancelled before this commit "
                    "reached the ledger",
                ),
                summarise(command.world, None),
            )
        held = await self._store.token_reservations(tx, command.commitment_token)
        owners = await self._store.token_missions(tx, command.commitment_token)
        foreign = [owner for owner in owners if mission is None or owner != mission.mission_id]
        if foreign:
            raise Declined(
                Rejection(
                    RejectionCode.IDENTITY_MISMATCH,
                    f"commitment token is already bound to mission {foreign[0]}",
                ),
                summarise(command.world, None),
            )
        protected = [row for row in held if row.state in protected_states()]
        if mission is not None and protected:
            # I2: the same request returns the same mission and the same commitment and
            # writes nothing at all. A retry after a successful commit is not a second
            # commitment and not a second charge.
            return CommitReceipt(
                mission_id=mission.mission_id,
                plan_version_id=mission.plan_version_id,
                commitment_token=command.commitment_token,
                reservation_ids=tuple(row.reservation_id for row in protected),
                decision_record_id=deterministic_id(command.commitment_token, "decision"),
                event_id=deterministic_id(command.commitment_token, "event", "committed"),
                committed=True,
                duplicate=True,
                code=RejectionCode.DUPLICATE_REQUEST_RESOLVED,
                detail=(
                    f"{len(protected)} protected reservation(s) already hold this commitment; "
                    "the original mission was returned"
                ),
                lock_order=_lock_sequence(row.resource_key for row in protected),
                checker=None,
                reservation_version=0,
            )
        steps.append(TraceStepRecord(CommitStep.VALIDATE_DUPLICATE_IDENTITY, "clear", f"order {order.order_id}"))

        # ---- step 2: freeze the world snapshot
        self._fault.fire(CommitStep.FREEZE_WORLD_SNAPSHOT)
        identities = _lock_sequence(
            claim.stored_key for plan in command.candidates for claim in plan.claims
        )
        world: WorldRead = await self._store.read_world(
            tx, identities, command.config_release_sha, command.policy_version
        )
        if world.config_release_id is None:
            raise Declined(
                Rejection(
                    RejectionCode.SNAPSHOT_NOT_FROZEN,
                    f"no config_release row for {command.config_release_sha} / "
                    f"{command.policy_version}: the inputs of this plan were never frozen",
                ),
                summarise(command.world, None),
            )
        frozen = _freeze(command.world, world, identities)
        gate = frozen.gate()
        if gate is not None:
            raise Declined(gate, summarise(frozen, None))
        steps.append(TraceStepRecord(CommitStep.FREEZE_WORLD_SNAPSHOT, "clear", frozen.snapshot_digest[:12]))

        # ---- steps 3 to 6: rank, generate, reject restricted, energy and recovery
        self._fault.fire(CommitStep.RANK_ELIGIBLE_AIRCRAFT)
        self._fault.fire(CommitStep.GENERATE_CANDIDATE_ROUTES)
        self._fault.fire(CommitStep.REJECT_RESTRICTED_CANDIDATES)
        self._fault.fire(CommitStep.APPLY_ENERGY_AND_RECOVERY_CHECKS)
        outcome = preflight(command.with_world(frozen))
        if isinstance(outcome, PreflightFailure):
            raise Declined(outcome.rejection, summarise(frozen, None))
        selection: Selection = outcome
        steps.append(
            TraceStepRecord(
                CommitStep.APPLY_ENERGY_AND_RECOVERY_CHECKS,
                "clear",
                f"candidate {selection.candidate.candidate_id} on aircraft {selection.aircraft.aircraft_id}",
            )
        )

        # ---- step 7: acquire row locks in canonical resource order
        self._fault.fire(CommitStep.ACQUIRE_ROW_LOCKS)
        claims = in_lock_order(selection.ordered_claims)
        locks = tuple(identity_of(claim.stored_key) for claim in claims)
        try:
            await self._store.lock_resources(tx, locks)
        except LookupError as error:
            raise Declined(
                Rejection(
                    RejectionCode.RESOURCE_UNAVAILABLE, str(error), resource_key=locks[0] if locks else None
                ),
                summarise(frozen, selection),
            ) from error
        steps.append(TraceStepRecord(CommitStep.ACQUIRE_ROW_LOCKS, "clear", f"{len(locks)} lock(s)"))

        # ---- step 8: recheck committed reservations
        self._fault.fire(CommitStep.RECHECK_COMMITTED_RESERVATIONS)
        holders = await self._holders(tx, claims)
        overlap = _overlap_rejection(claims, holders)
        if overlap is not None:
            raise Declined(overlap, summarise(frozen, selection))
        version, holder_count = await self._store.claim_version(
            tx, [_kind(claim) for claim in claims], [_identifier(claim) for claim in claims]
        )
        if command.expected_reservation_version is not None and version != command.expected_reservation_version:
            raise Declined(
                Rejection(
                    RejectionCode.STALE_RESERVATION_VERSION,
                    f"ledger version is {version}; this plan was priced at "
                    f"{command.expected_reservation_version} with {holder_count} holder(s)",
                ),
                summarise(frozen, selection),
            )
        steps.append(TraceStepRecord(CommitStep.RECHECK_COMMITTED_RESERVATIONS, "clear", f"version {version}"))

        # ---- step 9, first half: mission, plan and *tentative* reservations, then the vote
        self._fault.fire(CommitStep.WRITE_OPERATIONAL_RECORDS)
        mission_id = derived_mission_id
        plan_version_id = deterministic_id(command.commitment_token, "plan")
        reservation_ids = tuple(
            deterministic_id(command.commitment_token, "reservation", claim.stored_key) for claim in claims
        )
        summary = summarise(frozen, selection)
        async with tx.savepoint():
            effective_mission_id = mission.mission_id if mission is not None else mission_id
            entry_status = mission.status if mission is not None else "draft"
            if entry_status not in _REOPENABLE:
                raise Declined(
                    Rejection(
                        RejectionCode.IDENTITY_MISMATCH,
                        f"order {order.order_id} already carries mission {effective_mission_id} "
                        f"in state {entry_status}; the ledger does not re-commit a mission",
                    ),
                    summary,
                )
            if mission is None:
                await self._insert_mission(
                    tx, command, effective_mission_id, selection.aircraft.aircraft_id, "draft"
                )
            await self._store.insert_plan_version(
                tx,
                plan_version_id,
                effective_mission_id,
                selection.candidate.route_digest,
                self._profile_json(command, selection),
                selection.aircraft.energy.margin_wh,
                command.config_release_sha,
            )
            for claim, reservation_id in zip(claims, reservation_ids, strict=True):
                await self._store.insert_reservation(
                    tx,
                    reservation_id,
                    effective_mission_id,
                    claim.stored_key,
                    _kind(claim),
                    claim.valid_from,
                    claim.valid_until,
                    command.commitment_token,
                    command.config_release_sha,
                )
            await self._advance_mission(
                tx, effective_mission_id, plan_version_id, start=entry_status, stop="reserved_pending_commit"
            )
            # The vote is taken over the set *as stored*. It can only happen after the
            # tentative INSERT, and it can only be undone by the savepoint.
            final = FinalSet(
                requested=claims,
                holders=holders,
                policy_version=command.policy_version,
                config_release_sha=command.config_release_sha,
                candidate=selection.candidate,
            )
            vote: ValidationOutcome = self._vote(final)
            blocked = promotion(vote, command)
            if blocked is not None:
                raise Declined(blocked, summary, vote)
            promoted = await self._store.promote(tx, reservation_ids)
            if promoted != len(reservation_ids):
                raise InternalFault(
                    f"promoted {promoted} of {len(reservation_ids)} tentative rows; a reservation "
                    "moved under the ledger's own lock"
                )
            await self._advance_mission(
                tx, effective_mission_id, plan_version_id, start="reserved_pending_commit", stop="accepted"
            )
        steps.append(
            TraceStepRecord(CommitStep.WRITE_OPERATIONAL_RECORDS, "clear", f"{len(claims)} window(s) committed")
        )

        # ---- step 9, second half: the decision record that explains the promotion
        trace = DecisionTrace(
            command=command,
            input_hash=input_hash(command, summary),
            reason_codes=(RejectionCode.COMMITTED,),
            steps=tuple(steps),
            ranking=selection.ranking_rows(),
            rejections=selection.rejected,
            outcome=vote,
            lock_order=locks,
            reservation_version=version,
            committed=True,
        )
        decision_id = deterministic_id(command.commitment_token, "decision")
        await self._write_decision(tx, command, effective_mission_id, plan_version_id, decision_id, trace)

        # ---- step 10: append the outbox event
        self._fault.fire(CommitStep.APPEND_OUTBOX_EVENT)
        event_id = deterministic_id(command.commitment_token, "event", "committed")
        await self._append_event(
            tx, command, effective_mission_id, event_id, trace, RejectionCode.COMMITTED, reservation_ids
        )

        # ---- step 11: commit everything together (the caller's ``async with`` exit)
        self._fault.fire(CommitStep.COMMIT_OR_ROLLBACK)
        return CommitReceipt(
            mission_id=effective_mission_id,
            plan_version_id=plan_version_id,
            commitment_token=command.commitment_token,
            reservation_ids=reservation_ids,
            decision_record_id=decision_id,
            event_id=event_id,
            committed=True,
            duplicate=False,
            code=RejectionCode.COMMITTED,
            detail=f"committed {len(claims)} resource window(s) in canonical lock order",
            lock_order=locks,
            checker=vote,
            reservation_version=version,
        )

    # ------------------------------------------------------------------ rejection writing
    async def _persist_rejection(self, tx: Transaction, command: CommitCommand, refusal: Declined) -> None:
        """Record *why*, in the same transaction, with no operational row behind it.

        A rejection is a first-class result (conventions section 11): it persists a
        decision record carrying the reason code, the accepted-inputs hash, the candidate
        ranking and the configuration release, then emits its event. It writes no
        reservation row, and after a validator block it writes no mission hop either -
        the savepoint already took the tentative rows with it.
        """
        existing = await self._store.find_mission_by_order(tx, command.order_id)
        mission_id = (
            existing.mission_id
            if existing is not None
            else deterministic_id(str(command.order_id), "mission")
        )
        if existing is None and await self._store.mission_status(tx, mission_id) is None:
            # The anchor row is ``draft``, not ``failed``: what failed is this attempt, and the
            # decision record is what says so. ``failed`` is a terminal mission state and would
            # forbid the same order from ever committing - conventions section 9.
            await self._insert_mission(tx, command, mission_id, None, "draft")
        rejection = refusal.rejection
        detail = rejection.detail if refusal.vote is None else f"{rejection.detail}"
        trace = DecisionTrace(
            command=command,
            input_hash=input_hash(command, refusal.summary),
            reason_codes=rejection.reason_codes,
            steps=(TraceStepRecord(CommitStep.COMMIT_OR_ROLLBACK, "rejected", rejection.code.value),),
            rejections=(Rejection(rejection.code, detail, rejection.resource_key, rejection.candidate_id, rejection.aircraft_id),),
            outcome=refusal.vote,
            lock_order=_lock_sequence(
                claim.stored_key for plan in command.candidates for claim in in_lock_order(plan.claims)
            ),
        )
        decision_id = deterministic_id(str(mission_id), "decision", rejection.code.value)
        await self._write_decision(tx, command, mission_id, None, decision_id, trace)
        event_id = deterministic_id(str(mission_id), "event", rejection.code.value)
        await self._append_event(tx, command, mission_id, event_id, trace, rejection.code, ())

    async def _declined_receipt(
        self, tx: Transaction, command: CommitCommand, refusal: Declined
    ) -> CommitReceipt:
        existing = await self._store.find_mission_by_order(tx, command.order_id)
        mission_id = (
            existing.mission_id
            if existing is not None
            else deterministic_id(str(command.order_id), "mission")
        )
        rejection = refusal.rejection
        identities = _lock_sequence(claim.stored_key for plan in command.candidates for claim in plan.claims)
        version, _ = await self._store.claim_version(
            tx,
            sorted({identity.partition(":")[0] for identity in identities}),
            sorted({identity.partition(":")[1] for identity in identities}),
        )
        return CommitReceipt(
            mission_id=mission_id,
            plan_version_id=None,
            commitment_token=command.commitment_token,
            reservation_ids=(),
            decision_record_id=deterministic_id(str(mission_id), "decision", rejection.code.value),
            event_id=deterministic_id(str(mission_id), "event", rejection.code.value),
            committed=False,
            duplicate=False,
            code=rejection.code,
            detail=rejection.detail,
            lock_order=identities,
            checker=refusal.vote,
            reservation_version=version,
        )

    # ------------------------------------------------------------------ primitives
    async def _holders(self, tx: Transaction, claims: Sequence[ResourceClaim]) -> tuple[ProtectedHolding, ...]:
        unique: dict[tuple[str, UUID], ReservationRow] = {}
        for claim in claims:
            for row in await self._store.claim_holders(
                tx, _kind(claim), _identifier(claim), claim.valid_from, claim.valid_until
            ):
                unique[(row.resource_key, row.reservation_id)] = row
        return tuple(
            sorted(
                (_holding(row) for row in unique.values()),
                key=lambda holder: (identity_sort_key(identity_of(holder.resource_key)), holder.valid_from, holder.mission_id.hex),
            )
        )

    async def _insert_mission(
        self, tx: Transaction, command: CommitCommand, mission_id: UUID, aircraft_id: str | None, status: str
    ) -> None:
        await self._store.insert_mission(
            tx,
            mission_id,
            command.order_id,
            _aircraft_uuid(aircraft_id),
            status,
            None,
            command.priority_class.value,
            command.policy_version,
        )

    async def _advance_mission(
        self,
        tx: Transaction,
        mission_id: UUID,
        plan_version_id: UUID,
        *,
        start: str = "draft",
        stop: str = "reserved_pending_commit",
    ) -> None:
        """Walk the mission along ``mission_commit_path()``, one legal hop at a time.

        The hop-by-hop form is the point: a single ``UPDATE ... SET status='accepted'``
        would work just as well and would let a future edit skip
        ``reserved_pending_commit`` - which is exactly the ``planning -> activated`` class
        of bug conventions section 9 forbids.
        """
        labels = [state.value for state in mission_commit_path()]
        if start not in labels or stop not in labels or labels.index(start) > labels.index(stop):
            raise InternalFault(f"{start} -> {stop} is not on the ledger's commit path")
        for hop in labels[labels.index(start) + 1 : labels.index(stop) + 1]:
            assert_mission_can_be_accepted(hop)
            changed = await self._store.update_mission_status(
                tx, mission_id, labels[labels.index(hop) - 1], hop, plan_version_id
            )
            if changed != 1:
                raise InternalFault(
                    f"mission {mission_id} did not move to {hop}; another writer moved it underneath us"
                )

    async def _write_decision(
        self,
        tx: Transaction,
        command: CommitCommand,
        mission_id: UUID,
        plan_version_id: UUID | None,
        decision_id: UUID,
        trace: DecisionTrace,
    ) -> None:
        await self._store.insert_decision_record(
            tx,
            decision_id,
            mission_id,
            trace.input_hash,
            trace.eligible_json(),
            trace.rejected_json(),
            plan_version_id,
            [code.value for code in trace.reason_codes],
            command.actor,
            command.config_release_sha,
        )

    async def _append_event(
        self,
        tx: Transaction,
        command: CommitCommand,
        mission_id: UUID,
        event_id: UUID,
        trace: DecisionTrace,
        code: RejectionCode,
        reservation_ids: Sequence[UUID],
    ) -> None:
        seq = await self._store.next_event_seq(tx, "reservation", mission_id)
        await self._store.append_event(
            tx,
            event_id,
            "reservation",
            mission_id,
            seq,
            _EVENT_BY_CODE.get(code, _DECLINED_EVENT),
            trace.event_payload(reservation_ids),
            self._clock(),
            command.actor,
            command.config_release_sha,
            command.request_id,
            command.commitment_token,
        )

    def _profile_json(self, command: CommitCommand, selection: Selection) -> str:
        return PlanRecordPayload(
            strategy=selection.candidate.strategy,
            aircraft_id=selection.aircraft.aircraft_id,
            planner_verdict=selection.candidate.planner_verdict.value,
            policy_version=command.policy_version,
            config_release_sha=command.config_release_sha,
            points=selection.candidate.profile,
            claims=tuple(claim.stored_key for claim in selection.ordered_claims),
        ).as_json()


_NIL_ID: Final[UUID] = UUID(int=0)


def _freeze(world: WorldView, read: WorldRead, identities: Sequence[str]) -> WorldView:
    """Merge what the caller declared with what the database actually says.

    A resource that is not in the catalog at all is *unavailable*, not unrestricted: an
    unregistered volume is an unknown one, and unknown never opens airspace.
    """
    registered = {resource.resource_key for resource in read.resources}
    restricted = frozenset(
        resource.resource_key for resource in read.resources if resource.corridor_status != "open"
    ) | frozenset(world.restricted_keys)
    unavailable = tuple(
        sorted(
            {(resource.resource_key, resource.status) for resource in read.resources if resource.status != "available"}
            | {(identity, "unregistered") for identity in identities if identity not in registered}
            | set(world.unavailable)
        )
    )
    return replace(world, snapshot_digest=read.digest, restricted_keys=restricted, unavailable=unavailable)


def _lock_sequence(stored_keys: Iterable[str]) -> tuple[str, ...]:
    """Identities of these keys, deduplicated, in canonical acquisition order."""
    return resource_claim_identity_order(identity_of(key) for key in stored_keys)


def _trace_command(context: TenantContext, mission_id: UUID, token: str) -> CommitCommand:
    """A command-shaped record for operations that carry no plan (cancellation, I3).

    ``decision_record`` requires a mission and a configuration release; a cancellation has
    neither a candidate nor an aircraft, and inventing one would put false evidence in the
    trace.
    """
    world = WorldView(
        policy_version="ledger-operations",
        config_release_sha=context.config_release_sha,
        snapshot_digest="ledger-operations",
        reservation_version=0,
    )
    return CommitCommand(
        tenant_id=context.tenant_id,
        order_id=mission_id,
        mission_id=mission_id,
        plan_version_id=_NIL_ID,
        commitment_token=token,
        idempotency_key=token,
        policy_version="ledger-operations",
        config_release_sha=context.config_release_sha,
        actor=context.actor,
        world=world,
        aircraft=(),
        candidates=(),
    )


def _aircraft_uuid(value: str | None) -> UUID | None:
    if value is None:
        return None
    try:
        return UUID(value)
    except ValueError:
        return None


def _kind(claim: ResourceClaim) -> str:
    return claim.key.kind.name.lower()


def _identifier(claim: ResourceClaim) -> str:
    return claim.key.identifier


def _holding(row: ReservationRow) -> ProtectedHolding:
    """A stored holder plus the plan evidence an independent validator needs."""
    payload = PlanRecordPayload.from_json(row.profile_json) if row.profile_json else None
    return ProtectedHolding(
        resource_key=row.resource_key,
        mission_id=row.mission_id,
        state=row.state,
        valid_from=row.valid_from,
        valid_until=row.valid_until,
        points=() if payload is None else payload.points,
        plan_version=row.route_hash,
        policy_version="" if payload is None else payload.policy_version,
        planner_verdict=None if payload is None else payload.planner_verdict,
    )


def _overlap_rejection(
    claims: Sequence[ResourceClaim], holders: Sequence[ProtectedHolding]
) -> Rejection | None:
    """Step 8's answer, taken before the validator is asked anything.

    Cheap and decisive: a protected window already overlaps a requested one, so no
    geometry has to be re-derived to know this commit cannot proceed. The validator stays
    the authority on promotion; this is the guard that keeps the tentative write honest,
    and the only place an ``unresolved`` row can block a new commitment.
    """
    for claim in claims:
        for holder in holders:
            if identity_of(holder.resource_key) != claim.identity or not holder.blocking:
                continue
            stored = Interval.from_datetimes(holder.valid_from, holder.valid_until)
            if not stored.overlaps(claim.window):
                continue
            if holder.state == "unresolved":
                return Rejection(
                    RejectionCode.UNRESOLVED_PROTECTION,
                    f"{claim.identity} is held by unresolved reservation {holder.mission_id}; "
                    "telemetry loss does not clear airspace",
                    resource_key=holder.resource_key,
                )
            return Rejection(
                RejectionCode.COMMITTED_OVERLAP,
                f"{claim.identity} is already {holder.state} over {stored}",
                resource_key=holder.resource_key,
            )
    return None


__all__ = [
    "CancelReceipt",
    "CommitReceipt",
    "Declined",
    "ReservationCommitLedger",
    "utc_now",
]
