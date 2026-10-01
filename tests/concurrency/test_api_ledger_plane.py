"""The API on the real plane: ledger, checker adjacency and audit chain, over Postgres.

Every proof here starts at ``TestClient`` and ends in a SQL statement, because the claim
under test is that the control plane *commits* rather than *computes*: a response saying
airspace is held is worth nothing until a ``reservation`` row says so, and a refusal is
worth nothing until a ``decision_record`` carries its reason code.

Gap 1  two overlapping ``reserve`` calls - the second refused by the database-backed
       ledger, its reason code persisted, the first commitment read back as a row.
Gap 2  an adjacent-lane pair convicted through API -> ledger -> checker -> Postgres (the
       case ``no_ledger.committed_overlap`` cannot catch, because the resource keys differ), the
       same conviction reached through a frozen cell grid for ``ADJACENT_CELL``, and the
       ledger's ``committed_records`` port shown to be handed real grouped rows.
Gap 3  audit written into ``audit_log``: two appends chain, a direct ``UPDATE`` is refused
       by the database, ``droneops_app`` holds INSERT/SELECT only, and a concurrent append
       queues behind the tenant's advisory lock instead of forking the chain.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator, Sequence
from datetime import datetime, timezone
from typing import Any

import asyncpg
import pytest
from fastapi.testclient import TestClient
from ledger_support import (
    World,
    add_orders,
    candidate,
    cell_claim,
    command,
    corridor_claim,
    track_profile,
)

from droneops_api.composition import (
    build_checker_ledger,
    build_ledger_services,
    create_app,
    default_resolver,
)
from droneops_api.ledger_plane import LoopRunner
from droneops_api.reference import InMemoryControlPlane
from droneops_conflict_checker import CommittedPlanRecord
from droneops_platform.ports import AuditEntry, TenantContext
from droneops_persistence.audit_chain import PostgresAuditChain
from droneops_reservations.commit import FinalSet
from droneops_reservations.ledger import (
    LedgerStore,
    ReservationCommitLedger,
)
from droneops_reservations.ledger.validators import CheckerCommitValidator

pytestmark = pytest.mark.concurrency

HOSPITAL = "node-hospital-a"
CLINIC = "node-clinic-b"
#: Fixed and future: the ledger's window is the order's departure, so a proof cannot shift
#: meaning depending on the day it runs.
DEPARTURE = "2027-03-04T06:00:00Z"
DEADLINE = "2027-03-04T07:00:00Z"

TENANT_A = uuid.UUID("22222222-2222-2222-2222-222222222222")
GENESIS = "0" * 64
SHA = "sha256:" + "b" * 64

TRUNCATE = (
    'truncate table outbox, domain_event, decision_record, audit_log, reservation, '
    'plan_version, mission, "order", resource_catalog, corridor, config_release, pad, '
    "facility, aircraft, world_snapshot, sim_run, scenario, tenant restart identity cascade"
)


def resource_identity(key: str) -> str:
    """``<kind>:<identifier>`` of a stored key: a volume_cell key carries its window, so
    the identity is what a test can name without restating a range."""
    return ":".join(key.split(":")[:2])


def _now() -> datetime:
    return datetime.now(timezone.utc)


def order_body(key: str, destination: str) -> dict[str, Any]:
    return {
        "external_ref": f"partner-{key}",
        "origin_facility_id": "hub-north",
        "destination_facility_id": destination,
        "payload_profile_id": "diagnostic-kit-v1",
        "priority_class": "routine",
        "earliest_departure": DEPARTURE,
        "requested_deadline": DEADLINE,
        "idempotency_key": key,
    }


async def _truncate(dsn: str) -> None:
    """Clean the business tables and re-provision the tenant the credentials name.

    Audit rows have a real foreign key to ``tenant``, so the tenant is a first-class part
    of this world rather than something a test hopes the order path happened to insert.
    """
    connection = await asyncpg.connect(dsn, timeout=10)
    try:
        await connection.execute(TRUNCATE)
        await connection.execute(
            "insert into tenant (id, name, data_region, status) "
            "values ($1, 'ledger-plane-a', 'in-central', 'active') on conflict (id) do nothing",
            TENANT_A,
        )
    finally:
        await connection.close()


class LedgerApi:
    """The real bundle behind a client, plus the SQL handle that decides whether it worked."""

    def __init__(
        self, client: TestClient, runner: LoopRunner, dsn: str, sink: PostgresAuditChain
    ) -> None:
        self.client = client
        self.runner = runner
        self.sink = sink
        self._dsn = dsn

    def headers(self, token: str, key: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}", "Idempotency-Key": key}

    def order(self, key: str, destination: str) -> dict[str, Any]:
        response = self.client.post(
            "/v1/orders", json=order_body(key, destination), headers=self.headers("sim-partner-a", key)
        )
        assert response.status_code == 201, response.text
        return dict(response.json())

    def plan(self, mission_id: str, key: str, sha: str) -> list[dict[str, Any]]:
        response = self.client.post(
            f"/v1/missions/{mission_id}/plan",
            json={
                "config_release_sha": sha,
                "idempotency_key": key,
                "reason_code": "operator_requests_plan",
            },
            headers=self.headers("sim-scheduler-a", key),
        )
        assert response.status_code == 202, response.text
        return list(response.json()["items"])

    def reserve(self, mission_id: str, plan_version_id: str, key: str, sha: str) -> Any:
        return self.client.post(
            f"/v1/missions/{mission_id}/reserve",
            json={
                "plan_version_id": plan_version_id,
                "config_release_sha": sha,
                "idempotency_key": key,
                "reason_code": "operator_commits_plan",
            },
            headers=self.headers("sim-scheduler-a", key),
        )

    def commit(self, key: str, destination: str, sha: str) -> dict[str, Any]:
        """Order, plan, reserve: the documented happy path, one call from a test."""
        order = self.order(key, destination)
        mission_id = str(order["mission_id"])
        versions = self.plan(mission_id, f"{key}-plan", sha)
        response = self.reserve(mission_id, str(versions[0]["plan_version_id"]), f"{key}-reserve", sha)
        assert response.status_code == 201, response.text
        return {"order": order, "mission_id": mission_id, "reservations": response.json()["items"]}

    def query(self, sql: str, *args: object) -> list[dict[str, Any]]:
        async def read() -> list[dict[str, Any]]:
            connection = await asyncpg.connect(self._dsn, timeout=10)
            try:
                return [dict(row) for row in await connection.fetch(sql, *args)]
            finally:
                await connection.close()

        return self.runner.run(read())

    def execute(self, sql: str, *args: object) -> str:
        async def write() -> str:
            connection = await asyncpg.connect(self._dsn, timeout=10)
            try:
                return await connection.execute(sql, *args)
            finally:
                await connection.close()

        return self.runner.run(write())


@pytest.fixture
def ledger_api(postgres_dsn: str, checker_policy: Any) -> Iterator[LedgerApi]:
    """A client whose ``reserve`` runs the eleven steps, and whose audit trail is a table."""
    runner = LoopRunner()
    store = LedgerStore(postgres_dsn, pool_max=12)
    sink = PostgresAuditChain(postgres_dsn, pool_max=6)
    runner.run(store.open())
    runner.run(sink.open())
    runner.run(_truncate(postgres_dsn))
    services = build_ledger_services(
        plane=InMemoryControlPlane(),
        ledger=build_checker_ledger(store, checker_policy),
        store=store,
        runner=runner,
        audit_sink=sink,
        policy_version=checker_policy.policy_version,
    )
    app = create_app(services=services, resolver=default_resolver())
    try:
        yield LedgerApi(TestClient(app, raise_server_exceptions=False), runner, postgres_dsn, sink)
    finally:
        runner.run(sink.close())
        runner.run(store.close())
        runner.close()


def config_release_sha(client: TestClient) -> str:
    response = client.get("/version")
    assert response.status_code == 200
    return str(response.json()["config_release_sha"])


# --------------------------------------------------------------------- Gap 1
def test_overlapping_reserve_is_refused_by_the_ledger_and_persisted(ledger_api: LedgerApi) -> None:
    """The second overlapping request is refused by a transaction, not by a book.

    Two orders, one lane, the same minutes. The first commits; the second is refused by step
    8 reading *committed reservations in PostgreSQL*, and the refusal is persisted as a
    ``decision_record`` carrying ``ledger.committed_overlap``. The rows at the end are the point:
    without them the API has only ever described an airspace.
    """
    sha = config_release_sha(ledger_api.client)
    first = ledger_api.commit("k-overlap-a", HOSPITAL, sha)
    assert {resource_identity(item["resource_key"]) for item in first["reservations"]} == {
        "corridor:C-01",
        "volume_cell:V-01",
    }

    order = ledger_api.order("k-overlap-b", HOSPITAL)
    mission_id = str(order["mission_id"])
    versions = ledger_api.plan(mission_id, "k-overlap-b-plan", sha)
    refused = ledger_api.reserve(mission_id, str(versions[0]["plan_version_id"]), "k-overlap-b-reserve", sha)

    assert refused.status_code == 409, refused.text
    body = refused.json()
    assert "ledger.committed_overlap" in body["message"]

    rows = ledger_api.query(
        "select resource_key, state from reservation where tenant_id = $1 order by resource_key",
        TENANT_A,
    )
    assert sorted(resource_identity(row["resource_key"]) for row in rows) == [
        "corridor:C-01",
        "volume_cell:V-01",
    ]
    assert {row["state"] for row in rows} == {"committed"}

    codes = [
        code
        for record in ledger_api.query(
            "select reason_codes from decision_record where tenant_id = $1 order by id", TENANT_A
        )
        for code in record["reason_codes"]
    ]
    assert "ledger.committed" in codes
    assert "ledger.committed_overlap" in codes


# --------------------------------------------------------------------- Gap 2
def test_adjacent_lane_pair_is_convicted_through_api_ledger_checker_and_postgres(
    ledger_api: LedgerApi,
) -> None:
    """Adjacent lanes: different keys, so no constraint and no index can ever pair them.

    ``no_ledger.committed_overlap`` is an exclusion on ``resource_key WITH =``, and these two plans
    hold ``corridor:C-01`` and ``corridor:C-02`` - different strings, so Postgres is
    indifferent and correctly so. Step 8's identity recheck is indifferent too: a
    neighbouring lane is not this lane. The only thing that can convict this pair is the
    independent checker, over the adjacency-expanded holder pool the ledger now reads.
    """
    sha = config_release_sha(ledger_api.client)
    ledger_api.commit("k-lane-a", HOSPITAL, sha)

    order = ledger_api.order("k-lane-b", CLINIC)
    mission_id = str(order["mission_id"])
    versions = ledger_api.plan(mission_id, "k-lane-b-plan", sha)
    refused = ledger_api.reserve(mission_id, str(versions[0]["plan_version_id"]), "k-lane-b-reserve", sha)

    assert refused.status_code == 409, refused.text
    assert "checker.conflict" in refused.json()["message"], refused.text

    held = ledger_api.query(
        "select resource_key, state from reservation where tenant_id = $1 order by resource_key",
        TENANT_A,
    )
    assert sorted(resource_identity(row["resource_key"]) for row in held) == [
        "corridor:C-01",
        "volume_cell:V-01",
    ]

    conviction = ledger_api.query(
        "select reason_codes, rejected_candidates_json::text as rejected from decision_record "
        "where tenant_id = $1 and $2::text = any(reason_codes)",
        TENANT_A,
        "checker.conflict",
    )
    assert conviction, "the checker's verdict was never persisted"
    assert "corridor" in conviction[0]["rejected"]


class RecordingPool:
    """The ``committed_records`` port, logging what the ledger actually handed it.

    A port that is wired but never called is indistinguishable from ``None``, which is
    exactly how this gap stayed open: the default preserved the old behaviour, and nothing
    noticed. Wrapping it makes "did the ledger really supply the pool" a value.
    """

    def __init__(self) -> None:
        self._reader: Any = None
        self.calls: list[tuple[FinalSet, tuple[CommittedPlanRecord, ...]]] = []

    def bind(self, reader: Any) -> None:
        self._reader = reader

    def __call__(self, final: FinalSet) -> Sequence[CommittedPlanRecord]:
        records = () if self._reader is None else tuple(self._reader(final))
        self.calls.append((final, records))
        return records


async def test_adjacent_cell_is_convicted_through_the_commit_path_with_a_frozen_grid(
    store: LedgerStore, world: World, checker_policy: Any
) -> None:
    """``ADJACENT_CELL`` through promotion, with the cell grid frozen in the scenario world.

    The unit proof of ``ADJACENT_CELL`` existed; what was missing was a commit path able to
    reach it, because the ledger proofs froze no grid. ``V-01`` and ``V-02`` are declared
    neighbours and flown on neighbouring tracks over the same minutes. Step 8 cannot block
    it (a different cell is a different resource), the exclusion constraint cannot block it
    (a windowed key never matches), so if the adjacency read or the pool were absent this
    commit would promote - which is what makes the refusal the proof rather than the hope.

    The same run also proves the ``committed_records`` port is fed: one mission holds two
    resources, and the pool must hand the checker one record carrying both keys, not two
    records that could be mistaken for two strangers.
    """
    grid = {
        "corridor:C-01": ("corridor:C-02",),
        "corridor:C-02": ("corridor:C-01",),
        "volume_cell:V-01": ("volume_cell:V-02",),
        "volume_cell:V-02": ("volume_cell:V-01",),
    }
    pool = RecordingPool()
    validator = CheckerCommitValidator(checker_policy, _now(), adjacency=grid, committed_records=pool)
    ledger = ReservationCommitLedger(store, validator.validate, adjacency=grid, record_pool=True)
    pool.bind(ledger.committed_records)

    first_order, second_order = await add_orders(store, world, 2)
    first_claims = (
        corridor_claim(world.corridor, 0),
        cell_claim(world.volume_cell, 0),
    )
    first = command(
        world,
        "grid-first",
        claims=first_claims,
        candidates=(
            candidate(
                "cand-grid-first",
                "AC-001",
                first_claims,
                track_profile(first_claims[1].window, lane=world.volume_cell),
            ),
        ),
        order_id=first_order[0],
        idempotency_key=first_order[1],
    )
    won = await ledger.commit(world.context(), first)
    assert won.committed, won.explained

    second_claims = (cell_claim(world.other_volume_cell, 0),)
    second = command(
        world,
        "grid-second",
        claims=second_claims,
        candidates=(
            candidate(
                "cand-grid-second",
                "AC-001",
                second_claims,
                track_profile(second_claims[0].window, lane=world.other_volume_cell),
            ),
        ),
        order_id=second_order[0],
        idempotency_key=second_order[1],
    )
    blocked = await ledger.commit(world.context(), second)
    assert not blocked.committed, blocked.explained
    assert blocked.code.value == "checker.conflict", blocked.explained

    assert len(pool.calls) == 2, "the ledger did not supply the pool for both commits"
    _, records = pool.calls[1]
    assert len(records) == 1, f"expected one grouped record, got {records}"
    assert len(records[0].resource_keys) == 2, "the pool lost the mission's second resource"
    assert records[0].mission_id == str(won.mission_id)

    rows = await _reservation_rows(store, world.tenant_id)
    assert {row["resource_key"] for row in rows} == {claim.stored_key for claim in first_claims}


async def _reservation_rows(store: LedgerStore, tenant_id: uuid.UUID) -> list[dict[str, Any]]:
    connection = await store.pool.acquire()
    try:
        return [
            dict(row)
            for row in await connection.fetch(
                "select resource_key, state from reservation where tenant_id = $1", tenant_id
            )
        ]
    finally:
        await store.pool.release(connection)


# --------------------------------------------------------------------- Gap 3
def test_two_audit_writes_chain_and_the_database_refuses_a_rewrite(ledger_api: LedgerApi) -> None:
    """The audit trail is a table, the chain is real, and no one may rewrite it.

    Two API writes - one commitment, one refusal - become two ``audit_log`` rows whose
    ``prev_hash``/``record_hash`` link, verified by recomputing the canonical payload from
    the stored columns alone. Then the table owner attempts a plain ``UPDATE``, and the
    database refuses it: append-only is a property of the schema, not of this client's good
    intentions. The role grant is read out of the privilege catalog for the same reason.
    """
    sha = config_release_sha(ledger_api.client)
    ledger_api.commit("k-audit-a", HOSPITAL, sha)
    order = ledger_api.order("k-audit-b", HOSPITAL)
    mission_id = str(order["mission_id"])
    versions = ledger_api.plan(mission_id, "k-audit-b-plan", sha)
    ledger_api.reserve(mission_id, str(versions[0]["plan_version_id"]), "k-audit-b-reserve", sha)

    listed = ledger_api.client.get(
        # The record's instant is the wall clock, the plan's window is 2027: the read
        # is bounded by the former, and only the rows are bounded by the latter.
        "/v1/audit-events?time_from=2000-01-01T00:00:00Z&time_to=2100-01-01T00:00:00Z",
        headers={"Authorization": "Bearer sim-auditor-a"},
    )
    assert listed.status_code == 200, listed.text
    events = listed.json()["items"]
    assert len(events) >= 2, events
    assert events[0]["operation_kind"] == "reservation_committed"
    assert events[1]["prev_hash"] == events[0]["record_hash"]

    rows = ledger_api.query(
        "select action, prev_hash, record_hash from audit_log where tenant_id = $1 order by id",
        TENANT_A,
    )
    assert [row["action"] for row in rows] == ["reservation_committed", "commit_rejected"]
    assert rows[0]["prev_hash"] == GENESIS
    assert rows[1]["prev_hash"] == rows[0]["record_hash"]

    problems = ledger_api.runner.run(ledger_api.sink.verify(TENANT_A))
    assert problems == []

    # The trigger's own SQLSTATE: refusing a mutation on an append-only log is a
    # permission problem, not a data problem, and the database says so even to the owner
    # of the table - which is the whole point of putting the rule in a trigger.
    with pytest.raises(asyncpg.exceptions.PostgresError) as refused:
        ledger_api.execute("update audit_log set action = $2 where tenant_id = $1", TENANT_A, "rewritten")
    assert "append-only" in str(refused.value).lower()
    assert "audit_log" in str(refused.value).lower()

    with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
        ledger_api.execute("delete from audit_log where tenant_id = $1", TENANT_A)

    privileges = ledger_api.query(
        "select has_table_privilege('droneops_app', 'audit_log', 'INSERT') as can_insert, "
        "has_table_privilege('droneops_app', 'audit_log', 'SELECT') as can_select, "
        "has_table_privilege('droneops_app', 'audit_log', 'UPDATE') as can_update, "
        "has_table_privilege('droneops_app', 'audit_log', 'DELETE') as can_delete"
    )[0]
    assert privileges == {
        "can_insert": True,
        "can_select": True,
        "can_update": False,
        "can_delete": False,
    }


def test_concurrent_audit_appends_serialise_instead_of_forking_the_chain(
    ledger_api: LedgerApi,
) -> None:
    """Two appenders, one tenant, one instant: the chain lengthens, it does not fork.

    The chain guard takes ``pg_advisory_xact_lock(hashtext(tenant_id))`` on insert, and
    :meth:`PostgresAuditChain.append` takes the same key *before* reading the head it means
    to extend. Read-first-append-second would let both compute the same ``prev_hash`` and
    the guard would refuse the second - a correct but useless outcome. This asserts the
    better one: both rows land, the second extends the first, and the chain verifies.
    """
    context = TenantContext(
        tenant_id=TENANT_A, actor="audit-race", config_release_sha=SHA, request_id="audit-race"
    )
    entries = [
        _entry("audit-race-left", "reservation_committed", "committed"),
        _entry("audit-race-right", "commit_rejected", "checker.conflict"),
    ]

    async def append_both(sink: PostgresAuditChain) -> list[Any]:
        return list(await asyncio.gather(*(sink.append(context, entry) for entry in entries)))

    records = ledger_api.runner.run(append_both(ledger_api.sink))
    assert len(records) == 2
    assert records[0].record_hash != records[1].record_hash

    rows = ledger_api.query(
        "select prev_hash, record_hash, request_id from audit_log where tenant_id = $1 order by id",
        TENANT_A,
    )
    assert len(rows) == 2
    assert rows[0]["prev_hash"] == GENESIS
    assert rows[1]["prev_hash"] == rows[0]["record_hash"], "the chain forked"
    assert ledger_api.runner.run(ledger_api.sink.verify(TENANT_A)) == []


def _entry(request_id: str, action: str, reason_code: str) -> AuditEntry:
    return AuditEntry(
        tenant_id=TENANT_A,
        occurred_at=_now(),
        actor_type="service",
        actor_id="audit-race",
        action=action,
        resource_type="mission",
        resource_id=str(uuid.uuid5(uuid.NAMESPACE_OID, request_id)),
        request_id=request_id,
        reason_code=reason_code,
        config_release_sha=SHA,
    )
