"""Concurrency proofs for the reservation ledger, on real PostgreSQL connections.

``technical-build/06`` section 4 asks for exactly the five things below: 100 concurrent
commit attempts on one corridor, contention forced at the commit boundary, lock order
verified against deadlock, only valid non-conflicting commitments succeeding, and a
randomised fuzz pass over the committed set.

The measurements that matter here are not timings but *counts*: how many of the hundred
committed, how many were refused and for what reason, and whether any of the failures was
a database error leaking through instead of a decision. A ledger that "handled" a
deadlock by letting the driver raise has not handled anything: the caller cannot explain a
``DeadlockDetectedError`` to an operator, and it does not belong in a decision record.
"""

from __future__ import annotations

import asyncio
import random
import uuid
from collections import Counter

import asyncpg
import pytest
from ledger_support import World, add_orders, cell_claim, command, corridor_claim, make_ledger

from droneops_reservations.errors import RejectionCode
from droneops_reservations.ledger import LedgerStore, ReservationCommitLedger

pytestmark = pytest.mark.concurrency

REASONED = {
    RejectionCode.COMMITTED_OVERLAP,
    RejectionCode.UNRESOLVED_PROTECTION,
    RejectionCode.STALE_RESERVATION_VERSION,
    RejectionCode.CHECKER_CONFLICT,
    RejectionCode.CHECKER_UNPROVEN,
    RejectionCode.ALLOW_RULE_UNMATCHED,
    RejectionCode.CANDIDATE_RESTRICTED,
    RejectionCode.RESOURCE_UNAVAILABLE,
    RejectionCode.CANCELLED_BEFORE_COMMIT,
    RejectionCode.IDENTITY_MISMATCH,
    RejectionCode.DUPLICATE_REQUEST_RESOLVED,
}

LOCK = "update resource_catalog set label = label where tenant_id = $1 and resource_key = $2"


async def _gate(ready: asyncio.Event) -> None:
    """Every attempt waits here, then is released in the same instant."""
    ready.set()
    await ready.wait()


async def test_one_hundred_simultaneous_commits_over_one_corridor(
    store: LedgerStore, world: World
) -> None:
    """100 attempts, one corridor, 20 disjoint windows: exactly the safe subset wins.

    The committed set is asserted to be one reservation per distinct window requested and
    not one more, the database itself is asked whether any two protected rows overlap, and
    every refusal is checked to carry a reason the ledger chose rather than an error the
    driver produced.
    """
    seed = 20260930
    rng = random.Random(seed)
    slots = [rng.randrange(20) for _ in range(100)]
    tokens = [f"stress-{index}-{uuid.uuid4().hex[:8]}" for index in range(100)]
    ledger = make_ledger(store)
    ready = asyncio.Event()
    demands = await add_orders(store, world, len(tokens))
    tasks = [
        asyncio.create_task(_gate_then(ready, ledger, world, token, slot, *demand))
        for token, slot, demand in zip(tokens, slots, demands, strict=True)
    ]
    await asyncio.sleep(0)
    ready.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)

    exceptions = [item for item in results if isinstance(item, BaseException)]
    assert exceptions == [], f"database errors leaked through: {exceptions[:3]!r}"

    receipts = [item for item in results if not isinstance(item, BaseException)]
    assert len(receipts) == 100
    committed = [item for item in receipts if item.committed]
    refused = [item for item in receipts if not item.committed]

    assert {item.code for item in refused} <= REASONED, {item.code for item in refused}
    assert all(item.detail for item in refused)
    assert len(committed) == len(set(slots)), (
        f"expected one commitment per distinct window; seed={seed}, slots={Counter(slots)}"
    )
    assert len({item.reservation_ids[0] for item in committed}) == len(committed)

    violations = await ledger.policy_violations(world.context())
    assert violations == (), f"the committed set overlaps: {violations[:3]}"

    async with store.transaction(world.tenant_id) as tx:
        mission_ids = [item.mission_id for item in committed]
        counts = await store.counts_for(tx, mission_ids)
        states = {
            row.state
            for mission_id in mission_ids
            for row in await store.mission_rows(tx, mission_id)
        }
    assert states == {"committed"}
    assert counts.missions == len(mission_ids)
    assert counts.reservations == len(mission_ids)
    assert counts.decisions >= counts.reservations, "every commitment is explained"
    assert counts.events >= counts.reservations, "every commitment emitted"
    assert counts.outbox == counts.events, "an event without an outbox row is a lost publication"


async def _gate_then(
    ready: asyncio.Event,
    ledger: ReservationCommitLedger,
    world: World,
    token: str,
    slot: int,
    order_id: uuid.UUID,
    idempotency_key: str,
):
    await ready.wait()
    return await ledger.commit(
        world.context(),
        command(
            world,
            token,
            claims=(corridor_claim(world.corridor, slot),),
            order_id=order_id,
            idempotency_key=idempotency_key,
        ),
    )


async def test_lock_inversion_produces_no_deadlock_and_a_valid_final_set(
    store: LedgerStore, world: World, postgres_dsn: str
) -> None:
    """Two commits that list the same resources in opposite order, plus the control.

    The inversion is real: the control half of this test locks the same two rows in
    submission order from two connections and PostgreSQL reports ``DeadlockDetectedError``.
    The ledger half runs the same two requests through the ledger, where step 7 re-sorts
    into the canonical order, and asserts zero deadlocks and a valid final set. Without the
    control, "no deadlock" could just mean "nothing actually contended".
    """
    both = (corridor_claim(world.corridor, 30), cell_claim(world.volume_cell, 30))
    reversed_claims = (both[1], both[0])

    ledger_a = make_ledger(store)
    ledger_b = make_ledger(store)
    ready = asyncio.Event()
    first = asyncio.create_task(_commit_after(ready, ledger_a, world, "invert-a", both))
    second = asyncio.create_task(_commit_after(ready, ledger_b, world, "invert-b", reversed_claims))
    await asyncio.sleep(0)
    ready.set()
    results = await asyncio.gather(first, second, return_exceptions=True)

    deadlocks = [item for item in results if isinstance(item, asyncpg.DeadlockDetectedError)]
    errors = [item for item in results if isinstance(item, BaseException)]
    assert deadlocks == []
    assert errors == [], f"unexpected database errors: {errors[:3]!r}"
    receipts = [item for item in results if not isinstance(item, BaseException)]
    assert sum(1 for item in receipts if item.committed) == 2 or {
        item.code for item in receipts if not item.committed
    } <= REASONED
    violations = await ledger_a.policy_violations(world.context())
    assert violations == ()

    control_deadlocks = 0
    for _ in range(4):
        control_deadlocks += await _inverted_manual_lock_round(
            postgres_dsn, world.tenant_id, both[0].identity, both[1].identity
        )
    assert control_deadlocks >= 3, (
        "the inverted-order control did not deadlock, so the inversion above proves "
        f"nothing (deadlocks={control_deadlocks})"
    )


async def _commit_after(ready: asyncio.Event, ledger: ReservationCommitLedger, world: World, token: str, claims):
    await ready.wait()
    return await ledger.commit(world.context(), command(world, token, claims=claims))


async def _inverted_manual_lock_round(
    dsn: str, tenant_id: uuid.UUID, left: str, right: str
) -> int:
    """One round of deliberately inverted manual locking: 1 if Postgres deadlocked."""

    async def hold(connection: asyncpg.Connection, first: str, second: str) -> int:
        async with connection.transaction():
            await connection.execute("set local lock_timeout = '0'")
            await connection.execute("set local deadlock_timeout = '40ms'")
            await connection.fetch(LOCK, tenant_id, first)
            await asyncio.sleep(0.12)
            try:
                await connection.fetch(LOCK, tenant_id, second)
            except asyncpg.DeadlockDetectedError:
                return 1
            return 0

    a = await asyncpg.connect(dsn, timeout=3)
    b = await asyncpg.connect(dsn, timeout=3)
    # RLS filters every business table on ``app.tenant_id``; without the GUC the locking
    # statements would match zero rows and no contention - deadlocks=0 proves nothing.
    for connection in (a, b):
        await connection.execute("select set_config('app.tenant_id', $1, false)", str(tenant_id))
    try:
        results = await asyncio.gather(
            hold(a, left, right), hold(b, right, left), return_exceptions=True
        )
    finally:
        await a.close()
        await b.close()
    if any(isinstance(item, asyncpg.DeadlockDetectedError) for item in results):
        return 1
    return 0


async def test_twenty_simultaneous_duplicates_of_one_request_commit_once(
    store: LedgerStore, world: World
) -> None:
    """I2 under contention: a retry storm resolves to one mission and one commitment."""
    token = f"dupe-{uuid.uuid4().hex[:8]}"
    ledger = make_ledger(store)
    ready = asyncio.Event()
    tasks = [
        asyncio.create_task(_commit_after(ready, ledger, world, token, (corridor_claim(world.corridor, 31),)))
        for _ in range(20)
    ]
    await asyncio.sleep(0)
    ready.set()
    receipts = await asyncio.gather(*tasks)

    assert len({receipt.mission_id for receipt in receipts}) == 1
    assert len({receipt.reservation_ids for receipt in receipts}) == 1
    assert all(receipt.committed for receipt in receipts), [r.explained for r in receipts if not r.committed]

    async with store.transaction(world.tenant_id) as tx:
        counts = await store.counts_for(tx, [receipts[0].mission_id])
        rows, _ = await store.token_counts(tx, token)
    assert counts.reservations == 1, "one corridor, one reservation row - not one per retry"
    assert rows == 1
    assert counts.missions == 1


async def test_cancellation_racing_a_commit_resolves_one_way(
    store: LedgerStore, world: World
) -> None:
    """Requirement 8 under real contention: one of the two orders wins, never a half state."""
    token = f"race-{uuid.uuid4().hex[:8]}"
    ledger = make_ledger(store)
    mission_id = uuid.uuid5(uuid.NAMESPACE_OID, token)
    # The mission does not exist yet, so the cancellation cannot lock any resource row;
    # it takes the mission advisory key the ledger also takes at step 1.
    ready = asyncio.Event()

    async def do_commit() -> object:
        await ready.wait()
        return await ledger.commit(world.context(), command(world, token, claims=(corridor_claim(world.corridor, 32),)))

    async def do_cancel() -> object:
        await ready.wait()
        return await ledger.cancel(world.context(), mission_id, "cancel during commit")

    commit_task = asyncio.create_task(do_commit())
    cancel_task = asyncio.create_task(do_cancel())
    await asyncio.sleep(0)
    ready.set()
    commit_result, cancel_result = await asyncio.gather(commit_task, cancel_task, return_exceptions=True)

    assert not isinstance(commit_result, BaseException), commit_result
    assert not isinstance(cancel_result, BaseException), cancel_result
    assert commit_result.committed is True or cancel_result.cancelled, (
        f"neither party won: {commit_result!r} / {cancel_result!r}"
    )
    if commit_result.committed:
        assert cancel_result.commit_won or cancel_result.code is RejectionCode.MISSION_NOT_CANCELLABLE
        async with store.transaction(world.tenant_id) as tx:
            states = {row.state for row in await store.mission_rows(tx, commit_result.mission_id)}
        assert states <= {"committed"}, "the race released airspace"


async def test_seeded_fuzz_keeps_the_committed_set_valid(
    store: LedgerStore, world: World
) -> None:
    """Random keys and windows, twenty-five rounds: the invariant never breaks.

    The seed is recorded on failure, so a red run is reproducible rather than folklore
    (conventions section 10: no random value without a recorded seed).
    """
    rng = random.Random(4242)
    corridors = (world.corridor, world.other_corridor, "C-03")
    cells = (world.volume_cell, world.other_volume_cell)
    await _register_extra(store, world, ("corridor:C-03",))

    for round_index in range(25):
        attempts = []
        index = 0
        planned = rng.randint(6, 14)
        demand = await add_orders(store, world, planned)
        for _ in range(planned):
            slot = rng.randrange(0, 24)
            length = rng.randint(1, 6)
            claims = []
            if rng.random() < 0.8:
                claims.append(corridor_claim(rng.choice(corridors), slot, length_minutes=length))
            if rng.random() < 0.5:
                claims.append(cell_claim(rng.choice(cells), slot, length_minutes=length))
            if not claims:
                continue
            attempts.append(
                command(
                    world,
                    f"fuzz-{round_index}-{uuid.uuid4().hex[:8]}",
                    claims=claims,
                    order_id=demand[index][0],
                    idempotency_key=demand[index][1],
                )
            )
            index += 1

        ledger = make_ledger(store)
        receipts = await asyncio.gather(
            *(ledger.commit(world.context(), attempt) for attempt in attempts), return_exceptions=True
        )
        errors = [item for item in receipts if isinstance(item, BaseException)]
        assert errors == [], f"seed=4242 round={round_index}: {errors[:3]!r}"
        refused = [item for item in receipts if not item.committed]
        assert {item.code for item in refused} <= REASONED, {item.code for item in refused}

        violations = await ledger.policy_violations(world.context())
        assert violations == (), f"seed=4242 round={round_index} committed overlap: {violations[:4]}"


async def _register_extra(store: LedgerStore, world: World, identities: tuple[str, ...]) -> None:
    async with store.transaction(world.tenant_id) as tx:
        for identity in identities:
            await tx.execute(
                "insert into resource_catalog (resource_key, kind, tenant_id, capacity, status, source_release, label) "
                "values ($1, $2::resource_kind, $3, 1, 'available', $4, $5) "
                "on conflict (resource_key) do nothing",
                identity,
                identity.partition(":")[0],
                world.tenant_id,
                "sha256:" + "b" * 64,
                identity,
            )
