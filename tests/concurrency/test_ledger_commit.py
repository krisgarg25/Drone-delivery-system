"""Reservation-ledger proofs against real PostgreSQL.

Skipped, not faked, when the pinned container is unreachable - see ``conftest.py``.

What each test proves, and what it does not
    Every test below asserts a *state of the database* after the ledger ran, not a call
    sequence. A mock can prove the code issued an ``INSERT``; only Postgres can prove the
    committed set is valid, that a duplicate created no second commitment, and that a
    rollback left no orphan row (conventions section 11). None of these tests proves real
    aircraft behaviour - this is a simulator, and a commitment here is a statement about
    a resource window, not a clearance to fly.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

import pytest
from ledger_support import (
    BASE,
    World,
    add_order,
    aircraft,
    candidate,
    cell_claim,
    command,
    corridor_claim,
    make_ledger,
    outcome,
    profile,
    seed_world,
    window,
)

from droneops_conflict_checker import CheckerPolicy
from droneops_contracts import AircraftStatus
from droneops_reservations.commit import AllowRule, FinalSet, ValidationOutcome
from droneops_reservations.errors import RejectionCode
from droneops_reservations.intake import Verdict
from droneops_reservations.ledger import (
    IntervalCommitValidator,
    LedgerStore,
    ReservationCommitLedger,
)
from droneops_reservations.ledger.store import ReservationRow
from droneops_reservations.ledger.validators import CheckerCommitValidator

pytestmark = pytest.mark.concurrency


class StubValidator:
    """A validator with a fixed answer, for proofs whose subject *is* the verdict.

    Used where geometry would be a confound: UNPROVEN blocking, allow-rule agreement,
    promotion refusal. The verdict-shaped tests must not depend on whether a coincidence
    in the swept geometry happens to produce the same answer today.
    """

    name = "stub-validator"
    version = "0"

    def __init__(self, answer: ValidationOutcome) -> None:
        self._answer = answer
        self.seen: list[FinalSet] = []

    def validate(self, final: FinalSet) -> ValidationOutcome:
        self.seen.append(final)
        return self._answer


async def _rows(store: LedgerStore, tenant_id: uuid.UUID, mission_id: uuid.UUID) -> tuple[ReservationRow, ...]:
    async with store.transaction(tenant_id) as tx:
        return await store.mission_rows(tx, mission_id)


async def _decisions(store: LedgerStore, tenant_id: uuid.UUID, mission_id: uuid.UUID) -> Sequence[object]:
    async with store.transaction(tenant_id) as tx:
        return await store.decisions(tx, mission_id)


# ------------------------------------------------------------------ the happy path
async def test_a_commit_writes_the_whole_record_set_in_one_transaction(
    store: LedgerStore, world: World, ledger: ReservationCommitLedger
) -> None:
    """Steps 1-11 on the clean path: mission accepted, reservations committed, decision,
    event and outbox row - all present, all consistent, none orphaned."""
    receipt = await ledger.commit(world.context(), command(world, "commit-happy", slot=0))

    assert receipt.committed is True
    assert receipt.duplicate is False
    assert receipt.code is RejectionCode.COMMITTED
    assert receipt.lock_order == (f"corridor:{world.corridor}",)
    assert receipt.checker is not None and receipt.checker.verdict is Verdict.SAFE

    rows = await _rows(store, world.tenant_id, receipt.mission_id)
    assert [(row.resource_key, row.state) for row in rows] == [(f"corridor:{world.corridor}", "committed")]

    async with store.transaction(world.tenant_id) as tx:
        status = await store.mission_status(tx, receipt.mission_id)
        decisions = await store.decisions(tx, receipt.mission_id)
        events = await store.events(tx, receipt.mission_id)
        outbox = await store.outbox_for(tx, [event.event_id for event in events])
        counts = await store.counts_for(tx, [receipt.mission_id])

    assert status == "accepted", "the ledger walks the mission hop by hop to accepted"
    assert decisions[-1].reason_codes == (RejectionCode.COMMITTED.value,)
    assert decisions[-1].selected_plan_id == receipt.plan_version_id
    assert len(decisions[-1].input_hash) == 64
    assert [event.event_type for event in events] == ["reservation.committed"]
    assert len(outbox) == 1 and outbox[0][1] is False, "pending publication, not lost"
    assert (counts.reservations, counts.missions, counts.plans) == (1, 1, 1)
    assert (counts.decisions, counts.events, counts.outbox) == (1, 1, 1)


async def test_multiple_resources_are_locked_in_canonical_order_not_submission_order(
    store: LedgerStore, world: World
) -> None:
    """Step 7's order is the total order, whatever order the caller listed its claims in."""
    ledger = make_ledger(store)
    claims = (cell_claim(world.volume_cell, 4), corridor_claim(world.corridor, 4))
    receipt = await ledger.commit(world.context(), command(world, "commit-order", claims=claims))

    assert receipt.committed is True, receipt.explained
    assert receipt.lock_order == (f"corridor:{world.corridor}", f"volume_cell:{world.volume_cell}")
    rows = await _rows(store, world.tenant_id, receipt.mission_id)
    assert {row.resource_key.split(":")[0] for row in rows} == {"corridor", "volume_cell"}
    assert [row.state for row in rows] == ["committed", "committed"]


# ------------------------------------------------------------------ I2: idempotency
async def test_a_duplicate_request_returns_the_same_mission_and_writes_nothing(
    store: LedgerStore, world: World, ledger: ReservationCommitLedger
) -> None:
    """I2: a replay returns the same mission and the same commitment, with no second row."""
    first = await ledger.commit(world.context(), command(world, "idem-1", slot=1))
    assert first.committed is True

    async with store.transaction(world.tenant_id) as tx:
        before = await store.counts_for(tx, [first.mission_id])
        before_token = await store.token_counts(tx, first.commitment_token)

    replay = await ledger.commit(world.context(), command(world, "idem-1", slot=1))

    assert replay.committed is True
    assert replay.duplicate is True
    assert replay.code is RejectionCode.DUPLICATE_REQUEST_RESOLVED
    assert replay.mission_id == first.mission_id
    assert replay.plan_version_id == first.plan_version_id
    assert set(replay.reservation_ids) == set(first.reservation_ids)

    async with store.transaction(world.tenant_id) as tx:
        after = await store.counts_for(tx, [first.mission_id])
        after_token = await store.token_counts(tx, first.commitment_token)

    assert (after.reservations, after.missions, after.plans) == (
        before.reservations,
        before.missions,
        before.plans,
    )
    assert after.events == before.events, "no second event, so no second charge intent"
    assert after.outbox == before.outbox
    assert after_token == before_token


async def test_one_commitment_token_cannot_buy_two_orders(
    store: LedgerStore, world: World, ledger: ReservationCommitLedger
) -> None:
    """A token is bound to the order that minted it; replaying it elsewhere is a mismatch."""
    first = await ledger.commit(world.context(), command(world, "idem-2", slot=2))
    assert first.committed is True

    second_order, second_key = await add_order(store, world)
    hijack = command(
        world,
        "idem-2",
        slot=2,
        claims=(corridor_claim(world.other_corridor, 2),),
        order_id=second_order,
        idempotency_key=second_key,
    )
    receipt = await ledger.commit(world.context(), hijack)

    assert receipt.committed is False
    assert receipt.code is RejectionCode.IDENTITY_MISMATCH
    assert await _rows(store, world.tenant_id, receipt.mission_id) == ()


# ------------------------------------------------------------------ the checker votes
async def test_a_planner_approved_pair_the_checker_does_not_clear_is_never_committable(
    store: LedgerStore, world: World, checker_validator: CheckerCommitValidator
) -> None:
    """The Phase 2 "do not proceed" condition, executed rather than asserted in a comment.

    Two plans over the same minutes and the same ground track, one per adjacent lane. The
    interval view - which is what planner-side bucketing by resource key sees - calls that
    SAFE, because the two rows hold *different* resource keys. The independent checker,
    which sweeps the pair on its own construction, does not clear it. The ledger commits
    only after the checker's answer, so the disagreement is decided against the plan.
    """
    checker_ledger = make_ledger(store, checker_validator)
    first = await checker_ledger.commit(
        world.context(), command(world, "adjacent-first", claims=(corridor_claim(world.corridor, 6),))
    )
    assert first.committed is True, first.explained
    assert first.checker is not None and first.checker.verdict is Verdict.SAFE

    plan = candidate("adjacent-second", "AC-002", (corridor_claim(world.other_corridor, 6),), profile(6))
    interval_view = IntervalCommitValidator().validate(
        FinalSet(
            requested=plan.ordered_claims(),
            holders=(),
            policy_version=world.policy_version,
            config_release_sha=world.config_release_sha,
            candidate=plan,
        )
    )
    assert interval_view.verdict is Verdict.SAFE

    second_order, second_key = await add_order(store, world)
    receipt = await checker_ledger.commit(
        world.context(),
        command(
            world,
            "adjacent-second",
            candidates=(plan,),
            aircraft_candidates=(aircraft("AC-002"),),
            order_id=second_order,
            idempotency_key=second_key,
        ),
    )

    assert receipt.committed is False, "the ledger promoted a pair the checker did not clear"
    assert receipt.code in {
        RejectionCode.CHECKER_CONFLICT,
        RejectionCode.CHECKER_UNPROVEN,
        RejectionCode.ALLOW_RULE_UNMATCHED,
    }, receipt.explained
    assert receipt.checker is not None and receipt.checker.verdict is not Verdict.SAFE
    assert await _rows(store, world.tenant_id, receipt.mission_id) == ()

    async with store.transaction(world.tenant_id) as tx:
        decisions = await store.decisions(tx, receipt.mission_id)
        events = await store.events(tx, receipt.mission_id)
    assert decisions[-1].reason_codes == (receipt.code.value,)
    assert [event.event_type for event in events] == [
        "conflict.detected" if receipt.code is RejectionCode.CHECKER_CONFLICT else "checker.verdictRecorded"
    ]
    stored = decisions[-1].rejected_json
    assert str(stored).find("checker") != -1 or stored["checker"] is not None


async def test_unproven_blocks_promotion_even_when_the_plan_is_otherwise_clean(
    store: LedgerStore, world: World
) -> None:
    """``UNPROVEN`` is not a warning. Unknown never releases airspace."""
    ledger = make_ledger(store, StubValidator(outcome(Verdict.UNPROVEN, ("no altitude reference",))))
    receipt = await ledger.commit(world.context(), command(world, "unproven-1", slot=7))

    assert receipt.committed is False
    assert receipt.code is RejectionCode.CHECKER_UNPROVEN
    assert await _rows(store, world.tenant_id, receipt.mission_id) == ()
    async with store.transaction(world.tenant_id) as tx:
        events = await store.events(tx, receipt.mission_id)
        decisions = await store.decisions(tx, receipt.mission_id)
    assert [event.event_type for event in events] == ["checker.verdictRecorded"]
    assert decisions[-1].reason_codes == (RejectionCode.CHECKER_UNPROVEN.value,)
    assert "no altitude reference" in decisions[-1].rejected_json["rejections"][0]["detail"], (
        "the checker's own reason must be persisted verbatim, not paraphrased away"
    )


async def test_an_allow_rule_promotes_only_when_both_sides_name_the_same_version(
    store: LedgerStore, world: World
) -> None:
    """The only exception the ledger reads: explicitly named, versioned, and granted by
    the validator itself. An unnamed exception is a hole, not a rule."""
    granting = StubValidator(outcome(Verdict.CONFLICT, ("measured contact within envelope",), grant=("AR-014", "3")))

    unnamed = await make_ledger(store, granting).commit(world.context(), command(world, "allow-unnamed", slot=8))
    assert unnamed.committed is False
    # The checker's answer is a conflict; an exception the request never named cannot be
    # used to clear it, so the refusal carries the conflict code and says so in the detail.
    assert unnamed.code is RejectionCode.CHECKER_CONFLICT

    wrong_version = await make_ledger(store, granting).commit(
        world.context(), command(world, "allow-wrong", slot=8, allow_rule=("AR-014", "2"))
    )
    assert wrong_version.code is RejectionCode.ALLOW_RULE_UNMATCHED

    matched = await make_ledger(store, granting).commit(
        world.context(), command(world, "allow-matched", slot=9, allow_rule=("AR-014", "3"))
    )
    assert matched.committed is True, matched.explained
    assert matched.checker is not None and matched.checker.grant == AllowRule("AR-014", "3")
    assert await _rows(store, world.tenant_id, matched.mission_id) != ()


async def test_a_named_rule_never_overrides_an_unproven_answer(store: LedgerStore, world: World) -> None:
    """A rule permits a *known* overlap. It cannot turn "could not evaluate" into safe."""
    ledger = make_ledger(
        store, StubValidator(outcome(Verdict.UNPROVEN, ("missing provenance",), grant=("AR-014", "3")))
    )
    receipt = await ledger.commit(
        world.context(), command(world, "unproven-named", slot=10, allow_rule=("AR-014", "3"))
    )
    assert receipt.committed is False
    assert receipt.code is RejectionCode.CHECKER_UNPROVEN


# ------------------------------------------------------------------ steps 3 to 6
async def test_an_aircraft_that_cannot_recover_with_reserve_is_never_committed(
    store: LedgerStore, world: World
) -> None:
    """Step 6: the recovery gate rejects, and the rejection is in the trace, not in a log."""
    ledger = make_ledger(store)
    receipt = await ledger.commit(
        world.context(),
        command(
            world,
            "energy-1",
            aircraft_candidates=(aircraft("AC-010", recovery=False), aircraft("AC-011", margin_wh=91)),
            candidates=(
                candidate("c-energy-low", "AC-010", (corridor_claim(world.corridor, 11),), profile(11)),
                candidate("c-energy-ok", "AC-011", (corridor_claim(world.other_corridor, 11),), profile(11)),
            ),
        ),
    )
    assert receipt.committed is True
    assert receipt.lock_order == (f"corridor:{world.other_corridor}",)
    assert [(row.resource_key, row.state) for row in await _rows(store, world.tenant_id, receipt.mission_id)] == [
        (f"corridor:{world.other_corridor}", "committed")
    ]
    decisions = await _decisions(store, world.tenant_id, receipt.mission_id)
    codes = {item["code"] for item in decisions[-1].rejected_json["rejections"]}
    assert RejectionCode.RECOVERY_NOT_FEASIBLE.value in codes


async def test_offline_and_maintenance_aircraft_are_excluded_not_ranked_last(
    store: LedgerStore, world: World
) -> None:
    """I5: the candidate query removes them, so they cannot appear as eligible at all."""
    ledger = make_ledger(store)
    planes = (
        aircraft("AC-OFF", status=AircraftStatus.OFFLINE),
        aircraft("AC-MNT", status=AircraftStatus.MAINTENANCE),
        aircraft("AC-OK", lateness_us=5),
    )
    elig_claims = (corridor_claim(world.corridor, 12),)
    receipt = await ledger.commit(
        world.context(),
        command(
            world,
            "elig-1",
            slot=12,
            aircraft_candidates=planes,
            candidates=tuple(
                candidate(f"cand-{plane.aircraft_id}", plane.aircraft_id, elig_claims, profile(12)) for plane in planes
            ),
        ),
    )
    assert receipt.committed is True
    decisions = await _decisions(store, world.tenant_id, receipt.mission_id)
    assert [item["aircraft_id"] for item in decisions[-1].eligible_json["candidates"]] == ["AC-OK"]
    rejected = decisions[-1].rejected_json["rejections"]
    assert {item["aircraft_id"] for item in rejected} == {"AC-OFF", "AC-MNT"}


async def test_a_restricted_corridor_rejects_the_candidate_that_crosses_it(
    store: LedgerStore, checker_policy: CheckerPolicy
) -> None:
    """Step 5 reads the world the database holds, not the caller's assumption."""
    restricted = await seed_world(store, checker_policy, corridors=("C-09",), cells=("V-09",), restricted=("C-09",))
    ledger = make_ledger(store)
    receipt = await ledger.commit(
        restricted.context(),
        command(restricted, "restricted-1", claims=(corridor_claim("C-09", 13),)),
    )
    assert receipt.committed is False
    assert receipt.code in {RejectionCode.CANDIDATE_RESTRICTED, RejectionCode.RESOURCE_UNAVAILABLE}
    decisions = await _decisions(store, restricted.tenant_id, receipt.mission_id)
    assert decisions[-1].reason_codes[0] == receipt.code.value


# ------------------------------------------------------------------ I3: uncertainty
async def test_an_unresolved_reservation_still_protects_the_air_space(
    store: LedgerStore, world: World, ledger: ReservationCommitLedger
) -> None:
    """Section 19's "Telemetry loss" row, executed.

    Lost telemetry must not become empty airspace: the row keeps its protection, a new
    overlapping commitment is refused *because* of the uncertainty, and nothing in the
    database's exclusion index would have caught it - ``unresolved`` is outside that
    predicate by design.
    """
    first = await ledger.commit(world.context(), command(world, "i3-first", slot=14))
    assert first.committed is True

    assert await ledger.mark_unresolved(world.context(), first.mission_id, "link lost for 40 s") == 1
    assert [(row.resource_key, row.state) for row in await _rows(store, world.tenant_id, first.mission_id)] == [
        (f"corridor:{world.corridor}", "unresolved")
    ]

    second_order, second_key = await add_order(store, world)
    blocked = await ledger.commit(
        world.context(),
        command(
            world,
            "i3-second",
            claims=(corridor_claim(world.corridor, 14),),
            order_id=second_order,
            idempotency_key=second_key,
        ),
    )
    assert blocked.committed is False
    assert blocked.code is RejectionCode.UNRESOLVED_PROTECTION

    holders = await ledger.committed_overlapping(
        world.context(), f"corridor:{world.corridor}", *window(14).as_datetimes()
    )
    assert [holder.state for holder in holders] == ["unresolved"]
    assert await ledger.policy_violations(world.context()) == ()

    later_order, later_key = await add_order(store, world)
    later = await ledger.commit(
        world.context(),
        command(
            world,
            "i3-later",
            claims=(corridor_claim(world.corridor, 40),),
            order_id=later_order,
            idempotency_key=later_key,
        ),
    )
    assert later.committed is True, "an unresolved row protects its window, not the calendar"


async def test_cancellation_after_a_commitment_loses_the_race_deterministically(
    store: LedgerStore, world: World, ledger: ReservationCommitLedger
) -> None:
    """Requirement 8, ordering (a): commit wins, the airspace stays held, and the refusal
    is written down rather than swallowed."""
    first = await ledger.commit(world.context(), command(world, "race-first", slot=15))
    receipt = await ledger.cancel(world.context(), first.mission_id, "priority preempt")

    assert receipt.commit_won is True
    assert receipt.transitioned == 0 and receipt.declined == 1
    assert receipt.code is RejectionCode.CANCEL_REFUSED_ALREADY_COMMITTED
    assert [(row.state, row.resource_key) for row in await _rows(store, world.tenant_id, first.mission_id)] == [
        ("committed", f"corridor:{world.corridor}")
    ]
    async with store.transaction(world.tenant_id) as tx:
        assert await store.mission_status(tx, first.mission_id) == "accepted"
        codes = [code for decision in await store.decisions(tx, first.mission_id) for code in decision.reason_codes]
    assert codes[-1] == RejectionCode.CANCEL_REFUSED_ALREADY_COMMITTED.value
    assert await ledger.policy_violations(world.context()) == ()


async def test_cancellation_before_a_commitment_is_recorded_and_the_commit_refuses(
    store: LedgerStore, world: World, ledger: ReservationCommitLedger
) -> None:
    """Requirement 8, ordering (b): nothing was committed, so the cancellation is the truth."""
    token = "cancel-first"
    declined = await make_ledger(store, StubValidator(outcome(Verdict.CONFLICT, ("contact proven",)))).commit(
        world.context(), command(world, token, slot=16)
    )
    assert declined.committed is False
    assert await _rows(store, world.tenant_id, declined.mission_id) == ()

    receipt = await ledger.cancel(world.context(), declined.mission_id, "operator cancelled")
    assert receipt.code is RejectionCode.CANCELLED_BY_REQUEST

    again = await ledger.commit(world.context(), command(world, token, slot=16))
    assert again.committed is False
    assert again.code is RejectionCode.CANCELLED_BEFORE_COMMIT
    assert await _rows(store, world.tenant_id, again.mission_id) == ()


# ------------------------------------------------------------------ decision evidence
async def test_every_rejection_persists_reason_inputs_ranking_and_release(
    store: LedgerStore, world: World
) -> None:
    """Requirement 7: Phase 4 explains this from records, never by recomputation."""
    ledger = make_ledger(store)
    receipt = await ledger.commit(
        world.context(),
        command(world, "trace-1", slot=17, aircraft_candidates=(aircraft("AC-BAD", status=AircraftStatus.MAINTENANCE),)),
    )
    assert receipt.code is RejectionCode.NO_ELIGIBLE_AIRCRAFT

    async with store.transaction(world.tenant_id) as tx:
        decisions = await store.decisions(tx, receipt.mission_id)
        events = await store.events(tx, receipt.mission_id)
        outbox = await store.outbox_for(tx, [event.event_id for event in events])
        reservations = await store.mission_rows(tx, receipt.mission_id)

    assert reservations == (), "a rejection writes no airspace"
    assert decisions, "a rejection that is not written down is not explainable"
    decision = decisions[0]
    assert decision.reason_codes == (RejectionCode.NO_ELIGIBLE_AIRCRAFT.value,)
    assert len(decision.input_hash) == 64
    assert decision.config_release_sha == world.config_release_sha
    assert decision.deciding_actor == "ledger-test"
    assert decision.selected_plan_id is None
    assert decision.eligible_json["candidates"] == []
    rejection = decision.rejected_json["rejections"][0]
    assert rejection["code"] == RejectionCode.NO_ELIGIBLE_AIRCRAFT.value
    assert [event.event_type for event in events] == ["proposal.declined"]
    assert outbox and all(published is False for _, published in outbox)


async def test_the_ranking_and_lock_order_are_reproducible_from_the_stored_trace(
    store: LedgerStore, world: World
) -> None:
    """The same request, twice declined, produces byte-identical evidence."""
    from droneops_contracts import AircraftStatus as _Status

    ledger = make_ledger(store)
    candidates = (
        aircraft("AC-A", lateness_us=9, margin_wh=40),
        aircraft("AC-B", lateness_us=9, margin_wh=60),
        aircraft("AC-C", status=_Status.OFFLINE),
    )
    first = await ledger.commit(
        world.context(),
        command(world, "trace-order", slot=18, aircraft_candidates=candidates, candidates=()),
    )
    assert first.committed is False
    decisions = await _decisions(store, world.tenant_id, first.mission_id)
    ranking_a = decisions[0].eligible_json["candidates"]
    assert first.lock_order == ()


async def test_no_command_surface_reaches_the_ledger() -> None:
    """Requirement 9: the ledger decides resources, never aircraft behaviour."""
    public = {name for name in dir(ReservationCommitLedger) if not name.startswith("_")}
    forbidden = {
        name
        for name in public
        if any(
            word in name.lower()
            for word in ("command", "upload", "fly", "launch", "param", "firmware", "takeoff", "send", "inject")
        )
    }
    assert forbidden == set()
    assert {"commit", "cancel", "mark_unresolved", "committed_overlapping", "policy_violations", "decisions"} <= public


# ------------------------------------------------------------------ fixtures
@pytest.fixture
def checker_validator(
    checker_policy: CheckerPolicy, world: World
) -> CheckerCommitValidator:
    """The real adversarial checker, with the seeded lanes declared adjacent.

    ``adjacency`` is the lane map the world model would supply. Naming C-01 and C-02 as
    neighbours is what gives the checker's adjacent-corridor inspection something to
    compare; with no lane map it reasons only from geometry.
    """
    return CheckerCommitValidator(
        checker_policy,
        BASE,
        adjacency={world.corridor: (world.other_corridor,), world.other_corridor: (world.corridor,)},
    )
