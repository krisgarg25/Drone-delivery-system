"""Rollback proofs: force a failure at each of the eleven steps, in turn.

``technical-build/06`` section 4: "verify every rollback leaves no orphan reservation".
The only way to verify that is to break each step deliberately and then ask the database
what it holds. The fault program is the ledger's only failure-injection seam, it is
constructed explicitly by these tests, and no environment variable or route can reach it.

A rejection is *not* a rollback and is tested separately in ``test_ledger_commit.py``: a
refusal keeps its decision record, a fault keeps nothing. Both behaviours are required,
and a test that conflated them would hide exactly the bug this file is about - a
half-written explanation left behind by a crash.
"""

from __future__ import annotations

import uuid

import asyncpg
import pytest
from droneops_conflict_checker import CheckerPolicy
from droneops_contracts.errors import ErrorResponse  # noqa: F401 - proves contracts is importable here

from droneops_reservations.errors import RejectionCode, StepFault
from droneops_reservations.protocol import CommitStep
from droneops_reservations_ledger import LedgerStore, ReservationCommitLedger

from conftest import World, cell_claim, command, corridor_claim, make_ledger, seed_world

pytestmark = pytest.mark.concurrency


async def _counts(store: LedgerStore, tenant_id: uuid.UUID, token: str) -> tuple[int, ...]:
    """Everything a single commit attempt could have written, for one commitment token."""
    mission_id = uuid.UUID(int=0)
    async with store.transaction(tenant_id) as tx:
        rows = await store.token_reservations(tx, token)
        mission_ids = {row.mission_id for row in rows} | {mission_id}
        counts = await store.counts_for(tx, sorted(mission_ids))
    return (
        counts.reservations,
        counts.missions,
        counts.plans,
        counts.decisions,
        counts.events,
        counts.outbox,
    )


@pytest.mark.parametrize("step", list(CommitStep))
async def test_forcing_failure_at_each_step_leaves_nothing_behind(
    store: LedgerStore, checker_policy: CheckerPolicy, step: CommitStep
) -> None:
    """The eleven-step transaction is atomic at every step, not just at the last one."""
    world = await seed_world(store, checker_policy)
    token = f"fault-{step.value}"
    ledger = make_ledger(store, fail_at=step)

    with pytest.raises(StepFault):
        await ledger.commit(
            world.context(),
            command(world, token, claims=(corridor_claim(world.corridor, 3), cell_claim(world.volume_cell, 3))),
        )

    assert await _counts(store, world.tenant_id, token) == (0, 0, 0, 0, 0, 0), (
        f"step {step.number} ({step.description}) left operational or explanatory rows"
    )
    assert await ledger.decisions(world.context(), _mission_for(token)) == ()

    # The locks are gone with the transaction: the same resources commit cleanly next.
    healthy = make_ledger(store)
    receipt = await healthy.commit(world.context(), command(world, f"after-{step.value}", slot=3))
    assert receipt.committed is True, receipt.explained
    assert receipt.code is RejectionCode.COMMITTED


async def test_a_conflict_the_ledger_did_not_expect_is_still_a_rollback(
    store: LedgerStore, world: World
) -> None:
    """A real driver error, not an injected one: nothing is half-written.

    The row is planted by ``INSERT``-ing straight into the table, which is what an
    out-of-band writer or an application bug would do. The ledger's step-8 recheck refuses
    first, so the constraint is not what saves it - and the decision record that explains
    the refusal is still written, because a reasoned rejection is a result.
    """
    ledger = make_ledger(store)
    planted = uuid.uuid4()
    async with store.transaction(world.tenant_id) as tx:
        await tx.execute(
            "insert into mission (id, order_id, tenant_id, status, priority_class, policy_version) "
            "values ($1, $2, $3, 'accepted', 'routine', $4)",
            planted,
            world.order_id,
            world.tenant_id,
            world.policy_version,
        )
        await tx.execute(
            "insert into reservation (id, tenant_id, mission_id, resource_key, resource_kind, "
            "valid_during, state, commitment_token, config_release_sha) "
            "values ($5, $3, $1, $6, 'corridor', tstzrange($7, $8, '[)'), 'committed', $9, $10)",
            planted,
            world.order_id,
            world.tenant_id,
            world.policy_version,
            uuid.uuid4(),
            f"corridor:{world.corridor}",
            *command(world, "planted", slot=9).candidates[0].ordered_claims()[0].window.as_datetimes(),
            "planted-token",
            world.config_release_sha,
        )

    blocked = await ledger.commit(
        world.context(), command(world, "after-plant", claims=(corridor_claim(world.corridor, 9),))
    )
    assert blocked.committed is False
    assert blocked.code is RejectionCode.COMMITTED_OVERLAP

    rows = await ledger.committed_overlapping(
        world.context(), f"corridor:{world.corridor}", *command(world, "x", slot=9).candidates[0].ordered_claims()[0].window.as_datetimes()
    )
    assert [row.state for row in rows] == ["committed"]
    assert await ledger.policy_violations(world.context()) == ()


async def test_the_database_refuses_a_committed_overlap_written_past_the_ledger(
    store: LedgerStore, world: World
) -> None:
    """The last guard is real on the production table, not only on a canary.

    What this does *not* prove: that the constraint is sufficient. It indexes only
    ``('committed','activated')`` and compares ``resource_key`` for equality, so an
    ``unresolved`` row and a windowed key over the same cell are both invisible to it.
    Those cases are the ledger's step-8 recheck and the independent validator's job.
    """
    mission = uuid.uuid4()
    token = f"direct-{uuid.uuid4().hex[:8]}"

    async def insert(commitment: str) -> None:
        async with store.transaction(world.tenant_id) as tx:
            await tx.execute(
                "insert into mission (id, order_id, tenant_id, status, priority_class, policy_version) "
                "values ($1, $2, $3, 'accepted', 'routine', $4)",
                mission,
                world.order_id,
                world.tenant_id,
                world.policy_version,
            )
            start, end = command(world, commitment, slot=11).candidates[0].ordered_claims()[0].window.as_datetimes()
            await tx.execute(
                "insert into reservation (id, tenant_id, mission_id, resource_key, resource_kind, "
                "valid_during, state, commitment_token, config_release_sha) "
                "values ($1, $2, $3, $4, 'corridor', tstzrange($5, $6, '[)'), 'committed', $7, $8)",
                uuid.uuid4(),
                world.tenant_id,
                mission,
                f"corridor:{world.corridor}",
                start,
                end,
                commitment,
                world.config_release_sha,
            )

    await insert(token)
    with pytest.raises(asyncpg.ExclusionViolationError):
        await insert(f"{token}-again")


def _mission_for(token: str) -> uuid.UUID:
    """The mission id the ledger would have derived for this token."""
    from droneops_reservations.commit import deterministic_id

    return deterministic_id(token, "mission")
