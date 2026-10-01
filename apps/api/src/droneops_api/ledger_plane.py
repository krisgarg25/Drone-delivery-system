"""The ledger-backed plane: the API ports, wired to ``droneops_reservations.ledger``.

:mod:`droneops_api.reference` is a test double. This module is the wiring. A ``reserve``
made against the bundle :func:`droneops_api.composition.build_ledger_services` assembles
does not compute a plausible answer: it runs the eleven steps of ``technical-build/01``
section 9 inside one PostgreSQL transaction, promotes only when an independent validator
agrees, and leaves a ``decision_record`` behind whether it said yes or no.

What is real here, and what is still the double:

Real (database-backed)
    order intake, the reservation commit and its refusal, the committed ``reservation``
    rows, the ``decision_record`` reason codes, the mission state hops, the reservation
    reads, and the audit chain in ``audit_log``.

Still reference-level (named so, not hidden)
    the *route geometry*: which lane a plan flies, and therefore its claims and its
    two-point profile. A deployment swaps in the corridor graph and the planner; the ledger
    cannot tell the difference, because it re-derives protected volumes from the profile the
    plan carries rather than trusting the planner's memory. And the read models with no
    ledger counterpart yet - fleet, facilities, simulation runs, the status stream,
    decision traces, telemetry and proposals - are delegated to the double, which is why
    ``build_ledger_services`` takes a plane instead of replacing it.

``droneops_api`` must never import ``droneops_persistence`` (``.importlinter``
``api-has-no-command-path``). This module keeps that promise by reaching PostgreSQL only
through ``droneops_reservations.ledger`` - asyncpg and raw SQL, no ORM - and by reaching the
audit log through a protocol, :class:`AuditSink`, whose concrete implementation is injected.

The routers are synchronous, so every call here crosses into the database through
:class:`LoopRunner`: one event loop, one thread, one pool. ``asyncio.run`` per call would
work until the first pooled connection is created and then fail, because an ``asyncpg``
pool belongs to the loop that opened it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import uuid
from collections.abc import Callable, Coroutine, Mapping, Sequence
from concurrent.futures import Future
from typing import TypeVar
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol, cast

from droneops_contracts import AircraftStatus, AltitudeReference, LinkQuality, PriorityClass
from droneops_platform.ports import AuditEntry, AuditRecord, TenantContext
from droneops_reservations.intake import (
    AircraftCandidate,
    CandidatePlan,
    CommitCommand,
    EnergyVerdict,
    ProfileSample,
    Verdict,
    WorldView,
)
from droneops_reservations import deterministic_id
from droneops_reservations.protocol import ResourceClaim
from droneops_reservations.ledger import (
    CommitReceipt,
    LedgerStore,
    ReservationCommitLedger,
)

from droneops_api.ports import (
    AuditPort,
    CancelDraft,
    CommitDraft,
    ControlPlaneError,
    DuplicateCommitError,
    MissionPort,
    OrderDraft,
    OrderPort,
    PlanDraft,
    Principal,
    ProposalDraft,
    ReservationPort,
    ResourceNotFoundError,
)
from droneops_api.provenance import SourceKind, provenance_fields
from droneops_api.schemas import (
    AuditEventListResponse,
    AuditEventResponse,
    DecisionTraceResponse,
    MissionResponse,
    OrderResponse,
    OrderResponseList,
    PlanVersionResponse,
    PlanVersionResponseList,
    ReservationListResponse,
    ReservationResponse,
    ReservationStateValue,
    ResourceKindValue,
    TelemetryListResponse,
    UncertaintyStateResponse,
)

# ------------------------------------------------------------------ the frozen grid
#: The corridor lanes a control-plane plan may fly and the ground track each is drawn
#: along. ``C-01`` and ``C-02`` are ~19 m apart at this latitude, which is inside the two
#: plans' combined protected reach; ``C-03`` is ~380 m away, outside any reach. A far lane
#: is what keeps an adjacency conviction honest in both directions.
LANE_TRACKS: dict[str, tuple[tuple[float, float], tuple[float, float]]] = {
    "C-01": ((30.7500, 76.7800), (30.7700, 76.8000)),
    "C-02": ((30.7500, 76.7802), (30.7700, 76.8002)),
    "C-03": ((30.7500, 76.8200), (30.7700, 76.8400)),
}

#: The cell grid, one volume cell per lane, sharing the lanes' neighbourhood. A *windowed*
#: ``volume_cell`` key is the case the schema's exclusion constraint cannot even compare
#: (``resource_key WITH =`` sees a different string per window), so the grid is where an
#: adjacency conviction stops being a matter of naming.
CELL_BY_LANE: dict[str, str] = {"C-01": "V-01", "C-02": "V-02", "C-03": "V-03"}

#: lane -> neighbouring lanes. ``C-03`` has none, deliberately: adjacency is a frozen fact,
#: not a search radius.
LANE_ADJACENCY: dict[str, tuple[str, ...]] = {
    "C-01": ("C-02",),
    "C-02": ("C-01",),
    "C-03": (),
}
CELL_ADJACENCY: dict[str, tuple[str, ...]] = {
    "V-01": ("V-02",),
    "V-02": ("V-01",),
    "V-03": (),
}

#: Which lane a destination pulls an order onto. Anything unmapped takes the far lane, so
#: a route nobody modelled cannot be adjacent to one that was.
LANE_BY_DESTINATION: dict[str, str] = {
    "node-hospital-a": "C-01",
    "node-clinic-b": "C-02",
    "hub-north": "C-03",
}

_SEGMENT_MINUTES = 5
T = TypeVar("T", covariant=True)

_AUDIT_ACTOR_TYPES = frozenset({"user", "service", "checker", "break_glass"})
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_HORIZON = datetime(2100, 1, 1, tzinfo=timezone.utc)


def _digest(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def lane_of(destination: str) -> str:
    return LANE_BY_DESTINATION.get(destination, "C-03")


def frozen_adjacency() -> dict[str, tuple[str, ...]]:
    """The grid in the ledger's own key form - ``<kind>:<identifier>`` identities.

    This map is the whole of "what is next door" as far as a commitment is concerned: step 8
    expands a claim's holder read by these identities and by nothing else.
    """
    grid: dict[str, tuple[str, ...]] = {}
    for lane, neighbours in LANE_ADJACENCY.items():
        grid[f"corridor:{lane}"] = tuple(f"corridor:{other}" for other in neighbours)
    for cell, neighbours in CELL_ADJACENCY.items():
        grid[f"volume_cell:{cell}"] = tuple(f"volume_cell:{other}" for other in neighbours)
    return grid


def protected_volume_json(policy_version: str) -> str:
    """The frozen volume rule a ``config_release`` row must carry for step 2 to accept it."""
    return json.dumps(
        {
            "schema_version": 1,
            "policy_version": policy_version,
            "illustrative": True,
            "altitude_reference": "AGL",
            "horizontal_margin_m": 15.0,
            "vertical_margin_m": 10.0,
        },
        sort_keys=True,
    )


# ------------------------------------------------------------------ sync/async bridge
class LoopRunner:
    """One event loop on one thread, so synchronous routers can await one pooled database."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._serve, name="droneops-ledger-loop", daemon=True)
        self._started = threading.Event()
        self._thread.start()
        self._started.wait(timeout=10)

    def _serve(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._started.set()
        self._loop.run_forever()

    def submit(self, coroutine: Coroutine[Any, Any, T]) -> Future[T]:
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop)

    def run(self, coroutine: "Coroutine[Any, Any, T]") -> T:
        """Await ``coroutine`` on the plane's loop and hand back its result, typed.

        The generic is what keeps every adapter below honest: a call that returns
        ``CommitReceipt`` is not silently an ``object`` that someone casts later.
        """
        return self.submit(coroutine).result(timeout=120)

    def close(self) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)


# ------------------------------------------------------------------ injected audit sink
class AuditSink(Protocol):
    """What :class:`AuditChainPort` needs of the log.

    Structurally satisfied by ``droneops_persistence.audit_chain.PostgresAuditChain``,
    which this module may not name. The methods are coroutines on purpose: the adapter that
    owns the bridging is :class:`AuditChainPort`, and one bridging policy per process beats
    a protocol that looks synchronous and is not.
    """

    async def append(self, context: TenantContext, entry: AuditEntry) -> AuditRecord: ...

    async def window(
        self, context: TenantContext, time_from: datetime, time_to: datetime
    ) -> Sequence[AuditRecord]: ...

    async def verify(self, tenant_id: uuid.UUID) -> list[str]: ...


# ------------------------------------------------------------------ planning values
@dataclass
class OrderRecord:
    """The commit inputs one order produced, kept as values so a re-plan cannot retro-fit.

    ``plans`` is keyed by the plan version the API handed back, and ``reserve`` commits
    exactly what is stored there - the same claims, the same profile, the same window - so
    the row the ledger writes is the row the planner described.
    """

    order_id: uuid.UUID
    mission_id: uuid.UUID
    tenant_id: uuid.UUID
    idempotency_key: str
    origin: str
    destination: str
    priority_class: str
    earliest_departure: datetime
    requested_deadline: datetime
    config_release_sha: str
    policy_version: str
    plans: dict[uuid.UUID, PlannedRoute] = field(default_factory=dict)
    committed_token: str | None = None


@dataclass(frozen=True)
class PlannedRoute:
    plan_version_id: uuid.UUID
    candidate_id: str
    aircraft_id: str
    strategy: str
    route_digest: str
    lane: str
    cell: str
    claims: tuple[ResourceClaim, ...]
    profile: tuple[ProfileSample, ...]
    config_release_sha: str
    policy_version: str
    created_at: datetime
    energy_margin_wh: int = 90


@dataclass(frozen=True)
class PlaneWorld:
    """The rows a control-plane tenant needs before any commitment can be judged.

    A deployment that already imports the synthetic Chandigarh world does not use this; it
    exists so wiring the real ledger does not silently require a hand-seeded fixture. Every
    statement is ``on conflict do nothing``: seeding may never overwrite a fact an operator
    already set.
    """

    tenant_id: uuid.UUID
    facilities: tuple[tuple[uuid.UUID, str], ...]
    config_release_sha: str
    policy_version: str
    label: str = "phase-4-ledger-plane"

    @staticmethod
    def facility_id(tenant_id: uuid.UUID, name: str) -> uuid.UUID:
        return uuid.uuid5(uuid.NAMESPACE_OID, f"facility|{tenant_id}|{name}")


# ------------------------------------------------------------------ SQL
_INSERT_TENANT = """
insert into tenant (id, name, data_region, status)
values ($1, $2, 'in-central', 'active')
on conflict (id) do nothing
"""

_INSERT_FACILITY = """
insert into facility (id, tenant_id, name, kind, source)
values ($1, $2, $3, 'hub', 'phase-4-ledger-plane')
on conflict (id) do nothing
"""

_INSERT_CONFIG_RELEASE = """
insert into config_release (id, tenant_id, config_release_sha, label, policy_version,
                            protected_volume_json)
values ($1, $2, $3, $4, $5, $6::jsonb)
on conflict do nothing
"""

_INSERT_RESOURCE = """
insert into resource_catalog (resource_key, kind, tenant_id, capacity, status, source_release, label)
values ($1, $2::resource_kind, $3, 1, 'available', $4, $5)
on conflict (resource_key) do nothing
"""

_INSERT_ORDER = """
insert into "order" (id, tenant_id, external_ref_hash, origin_facility_id,
                     destination_facility_id, payload_profile_id, priority_class,
                     earliest_departure, requested_deadline, idempotency_key,
                     config_release_sha, status)
values ($1, $2, $3, $4, $5, $6, $7::priority_class, $8, $9, $10, $11, 'validated')
"""

_SELECT_ORDER = """
select id::text as row_id, status::text as status, priority_class::text as priority_class,
       external_ref_hash, earliest_departure, requested_deadline
  from "order"
 where tenant_id = $1 and id = $2
"""

_SELECT_ORDERS = """
select id::text as row_id, status::text as status, priority_class::text as priority_class,
       external_ref_hash, earliest_departure, requested_deadline
  from "order"
 where tenant_id = $1 and earliest_departure >= $2 and earliest_departure < $3
 order by earliest_departure, id
"""

_SELECT_RESERVATIONS = """
select id::text as reservation_id, mission_id::text as mission_id, resource_key,
       resource_kind::text as resource_kind, lower(valid_during) as valid_from,
       upper(valid_during) as valid_until, state::text as state, commitment_token
  from reservation
 where tenant_id = $1
   and upper(valid_during) > $2
   and lower(valid_during) < $3
   and mission_id = coalesce($4::uuid, mission_id)
 order by lower(valid_during), resource_key, id
"""


class LedgerPlane:
    """The shared state of the ledger-backed ports: one store, one ledger, one world.

    Orders and missions are tracked here because ``reserve`` needs the *order's* idempotency
    key (step 1 resolves the order through it and refuses when the two disagree) while the
    HTTP request carries the reserve key, which belongs to the idempotency ledger instead.
    Keeping both in one place is what stops a client key from being mistaken for an order.
    """

    def __init__(
        self,
        store: LedgerStore,
        ledger: ReservationCommitLedger,
        runner: LoopRunner,
        world: Callable[[Principal], PlaneWorld],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.store = store
        self.ledger = ledger
        self.runner = runner
        self.world = world
        self.clock = clock
        self._orders: dict[uuid.UUID, OrderRecord] = {}
        self._missions: dict[uuid.UUID, OrderRecord] = {}

    # ------------------------------------------------------------------ registry
    def order(self, order_id: uuid.UUID) -> OrderRecord | None:
        return self._orders.get(order_id)

    def mission(self, mission_id: uuid.UUID) -> OrderRecord | None:
        return self._missions.get(mission_id)

    def register(self, record: OrderRecord) -> None:
        self._orders[record.order_id] = record
        self._missions[record.mission_id] = record

    # ------------------------------------------------------------------ writes
    async def seed(self, world: PlaneWorld) -> None:
        async with self.store.transaction(world.tenant_id) as tx:
            await tx.execute(_INSERT_TENANT, world.tenant_id, f"tenant-{world.tenant_id.hex[:8]}")
            for facility_id, name in world.facilities:
                await tx.execute(_INSERT_FACILITY, facility_id, world.tenant_id, name)
            await tx.execute(
                _INSERT_CONFIG_RELEASE,
                uuid.uuid5(uuid.NAMESPACE_OID, f"release|{world.config_release_sha}|{world.policy_version}"),
                world.tenant_id,
                world.config_release_sha.removeprefix("sha256:"),
                world.label,
                world.policy_version,
                protected_volume_json(world.policy_version),
            )

    async def insert_order(
        self, world: PlaneWorld, record: OrderDraft, order_id: uuid.UUID, tenant_id: uuid.UUID
    ) -> None:
        await self.seed(world)
        async with self.store.transaction(tenant_id) as tx:
            await tx.execute(
                _INSERT_ORDER,
                order_id,
                tenant_id,
                _digest(record.external_ref),
                PlaneWorld.facility_id(tenant_id, record.origin_facility_id),
                PlaneWorld.facility_id(tenant_id, record.destination_facility_id),
                record.payload_profile_id,
                record.priority_class,
                record.earliest_departure,
                record.requested_deadline,
                record.idempotency_key,
                world.config_release_sha,
            )

    async def register_resources(self, tenant_id: uuid.UUID, claims: Sequence[ResourceClaim]) -> None:
        async with self.store.transaction(tenant_id) as tx:
            for claim in claims:
                kind = claim.key.kind.name.lower()
                await tx.execute(
                    _INSERT_RESOURCE,
                    claim.identity,
                    kind,
                    tenant_id,
                    f"sha256:{_digest(kind)}",
                    claim.identity,
                )

    async def fetch_reservations(
        self, tenant_id: uuid.UUID, time_from: datetime, time_to: datetime, mission_id: uuid.UUID | None
    ) -> Sequence[Mapping[str, object]]:
        async with self.store.transaction(tenant_id) as tx:
            return await tx.fetch(_SELECT_RESERVATIONS, tenant_id, time_from, time_to, mission_id)

    async def fetch_order(self, tenant_id: uuid.UUID, order_id: uuid.UUID) -> Mapping[str, object] | None:
        async with self.store.transaction(tenant_id) as tx:
            return await tx.fetchrow(_SELECT_ORDER, tenant_id, order_id)

    async def fetch_orders(
        self, tenant_id: uuid.UUID, time_from: datetime, time_to: datetime
    ) -> Sequence[Mapping[str, object]]:
        async with self.store.transaction(tenant_id) as tx:
            return await tx.fetch(_SELECT_ORDERS, tenant_id, time_from, time_to)

    # ------------------------------------------------------------------ planning
    def build_plan(self, record: OrderRecord, strategy: str, idempotency_key: str) -> PlannedRoute:
        """One candidate route: the lane the destination pulls onto, plus its cell.

        Reference-level geometry, honestly labelled: a deployment replaces ``lane_of`` with
        the corridor graph and the planner. What is *not* reference-level is the consequence
        - these claims and these raw profile points are exactly what the ledger stores and
        what the independent checker re-derives volumes from.
        """
        lane = lane_of(record.destination)
        start = record.earliest_departure
        stop = start + timedelta(minutes=_SEGMENT_MINUTES)
        plan_version_id = uuid.uuid5(
            uuid.NAMESPACE_OID, f"plan|{record.order_id}|{strategy}|{idempotency_key}"
        )
        return PlannedRoute(
            plan_version_id=plan_version_id,
            candidate_id=f"cand-{plan_version_id}",
            aircraft_id=f"AC-{lane}",
            strategy=strategy,
            route_digest=_digest(str(plan_version_id), strategy),
            lane=lane,
            cell=CELL_BY_LANE[lane],
            claims=(
                ResourceClaim.of("corridor", lane, start, stop),
                ResourceClaim.of("volume_cell", CELL_BY_LANE[lane], start, stop),
            ),
            profile=_track_profile(lane, start, stop),
            config_release_sha=record.config_release_sha,
            policy_version=record.policy_version,
            created_at=self.clock(),
        )

    def command(self, record: OrderRecord, plan: PlannedRoute, token: str, reason_code: str) -> CommitCommand:
        """The commit input, built from stored values only.

        ``idempotency_key`` is the *order's*: step 1 resolves the order through it and
        refuses when order and key disagree. ``aircraft_id`` is not a UUID, so
        ``mission.aircraft_id`` stays null - the fleet read model has no ledger row here,
        and inventing an identifier the aircraft table would not recognise is worse than
        recording nothing.
        """
        return CommitCommand(
            tenant_id=record.tenant_id,
            order_id=record.order_id,
            mission_id=record.mission_id,
            plan_version_id=plan.plan_version_id,
            commitment_token=token,
            idempotency_key=record.idempotency_key,
            policy_version=plan.policy_version,
            config_release_sha=plan.config_release_sha,
            actor=f"api:{reason_code}",
            world=WorldView(
                policy_version=plan.policy_version,
                config_release_sha=plan.config_release_sha,
                snapshot_digest="",
                reservation_version=0,
            ),
            aircraft=(_candidate(plan.aircraft_id, record.priority_class),),
            candidates=(_candidate_plan(plan),),
            request_id=str(uuid.uuid4()),
        )


class LedgerOrderPort(OrderPort):
    """Order intake that writes the row the ledger's step 1 insists on reading."""

    def __init__(self, plane: LedgerPlane) -> None:
        self._plane = plane

    def create(self, principal: Principal, draft: OrderDraft) -> OrderResponse:
        world = self._plane.world(principal)
        order_id = uuid.uuid5(uuid.NAMESPACE_OID, f"order|{draft.idempotency_key}|{principal.tenant_id}")
        record = OrderRecord(
            order_id=order_id,
            # The ledger's own derivation, so the mission the API names and the mission
            # the ledger inserts are the same row rather than two ids that look alike.
            mission_id=deterministic_id(str(order_id), "mission"),
            tenant_id=principal.tenant_id,
            idempotency_key=draft.idempotency_key,
            origin=draft.origin_facility_id,
            destination=draft.destination_facility_id,
            priority_class=draft.priority_class,
            earliest_departure=draft.earliest_departure,
            requested_deadline=draft.requested_deadline,
            config_release_sha=world.config_release_sha,
            policy_version=world.policy_version,
        )
        self._plane.runner.run(self._plane.insert_order(world, draft, order_id, principal.tenant_id))
        self._plane.register(record)
        return OrderResponse(
            order_id=record.order_id,
            tenant_id=record.tenant_id,
            mission_id=record.mission_id,
            status="validated",
            external_ref=draft.external_ref,
            priority_class=draft.priority_class,
            **provenance_fields(SourceKind.COMMITTED),
        )

    def fetch(self, principal: Principal, order_id: uuid.UUID) -> OrderResponse:
        record = self._plane.order(order_id)
        row = self._plane.runner.run(self._plane.fetch_order(principal.tenant_id, order_id))
        if record is None or record.tenant_id != principal.tenant_id or row is None:
            raise ResourceNotFoundError("Order not found")
        return self._response(principal.tenant_id, record, row)

    def list(self, principal: Principal, time_from: datetime, time_to: datetime) -> OrderResponseList:
        rows = self._plane.runner.run(self._plane.fetch_orders(principal.tenant_id, time_from, time_to))
        items = [
            self._response(
                principal.tenant_id,
                self._plane.order(uuid.UUID(str(row["row_id"]))),
                row,
            )
            for row in rows
        ]
        return OrderResponseList(items=items, **provenance_fields(SourceKind.COMMITTED))

    def cancel(self, principal: Principal, order_id: uuid.UUID, draft: CancelDraft) -> MissionResponse:
        record = self._plane.order(order_id)
        if record is None or record.tenant_id != principal.tenant_id:
            raise ResourceNotFoundError("Order not found")
        return self._cancel(principal, record, draft)

    @staticmethod
    def _response(
        tenant_id: uuid.UUID, record: OrderRecord | None, row: Mapping[str, object]
    ) -> OrderResponse:
        return OrderResponse(
            order_id=uuid.UUID(str(row["row_id"])),
            tenant_id=tenant_id,
            mission_id=record.mission_id if record is not None else uuid.UUID(int=0),
            status=str(row["status"]),
            external_ref=str(row["external_ref_hash"])[:16],
            priority_class=str(row["priority_class"]),
            **provenance_fields(SourceKind.COMMITTED),
        )

    def _cancel(self, principal: Principal, record: OrderRecord, draft: CancelDraft) -> MissionResponse:
        receipt = self._plane.runner.run(
            self._plane.ledger.cancel(context_of(principal, record.config_release_sha), record.mission_id, draft.reason_code)
        )
        if receipt.declined:
            raise DuplicateCommitError(
                f"Cancellation refused by the ledger ({receipt.code.value}): {receipt.detail}",
                str(record.mission_id),
            )
        return cancelled_response(record)


class LedgerMissionPort(MissionPort):
    """Planning values plus the real eleven-step commit; reads delegate to the double."""

    def __init__(
        self,
        plane: LedgerPlane,
        *,
        audit: Callable[[Principal, str, str, str], None] | None = None,
        fallback: MissionPort | None = None,
    ) -> None:
        self._plane = plane
        self._audit = audit
        self._fallback = fallback

    def plan(self, principal: Principal, mission_id: uuid.UUID, draft: PlanDraft) -> PlanVersionResponseList:
        record = self._require(principal, mission_id)
        routes = tuple(
            self._plane.build_plan(record, strategy, draft.idempotency_key)
            for strategy in ("lane-based", "restricted-avoidance")
        )
        claims: list[ResourceClaim] = []
        for route in routes:
            claims.extend(route.claims)
        self._plane.runner.run(self._plane.register_resources(principal.tenant_id, dedupe(claims)))
        record.plans = {route.plan_version_id: route for route in routes}
        return PlanVersionResponseList(
            items=[self._response(record, route) for route in routes],
            **provenance_fields(SourceKind.PLANNED),
        )

    def plan_versions(self, principal: Principal, mission_id: uuid.UUID) -> PlanVersionResponseList:
        record = self._require(principal, mission_id)
        return PlanVersionResponseList(
            items=[self._response(record, route) for route in record.plans.values()],
            **provenance_fields(SourceKind.PLANNED),
        )

    def reserve(self, principal: Principal, mission_id: uuid.UUID, draft: CommitDraft) -> ReservationListResponse:
        record = self._require(principal, mission_id)
        route = record.plans.get(draft.plan_version_id)
        if route is None:
            raise ResourceNotFoundError("Plan version not found for this mission")
        if draft.config_release_sha != route.config_release_sha:
            raise ControlPlaneError(
                "config_release_sha does not match the plan version; re-plan before committing.",
                error_code="unresolved",
            )
        token = commitment_token(mission_id, route.plan_version_id)
        receipt: CommitReceipt = self._plane.runner.run(
            self._plane.ledger.commit(
                context_of(principal, route.config_release_sha),
                self._plane.command(record, route, token, draft.reason_code),
            )
        )
        if not receipt.committed:
            # The refusal is already durable: the ledger persisted a decision_record with
            # this reason code in the same transaction. The API reports it, it does not
            # reconstruct it.
            self._note(principal, "commit_rejected", mission_id, receipt.code.value, route)
            raise DuplicateCommitError(
                f"Commit refused by the ledger ({receipt.code.value}): {receipt.detail}",
                str(receipt.decision_record_id),
            )
        record.committed_token = token
        self._note(principal, "reservation_committed", mission_id, receipt.code.value, route)
        rows = self._plane.runner.run(
            self._plane.fetch_reservations(principal.tenant_id, _EPOCH, _HORIZON, mission_id)
        )
        return ReservationListResponse(
            items=[reservation_response(principal.tenant_id, row) for row in rows],
            **provenance_fields(SourceKind.COMMITTED),
        )

    def reservations(self, principal: Principal, mission_id: uuid.UUID) -> ReservationListResponse:
        record = self._require(principal, mission_id)
        rows = self._plane.runner.run(
            self._plane.fetch_reservations(record.tenant_id, _EPOCH, _HORIZON, mission_id)
        )
        return ReservationListResponse(
            items=[reservation_response(record.tenant_id, row) for row in rows],
            **provenance_fields(SourceKind.COMMITTED),
        )

    def cancel(self, principal: Principal, mission_id: uuid.UUID, draft: CancelDraft) -> MissionResponse:
        record = self._require(principal, mission_id)
        receipt = self._plane.runner.run(
            self._plane.ledger.cancel(
                context_of(principal, record.config_release_sha), mission_id, draft.reason_code
            )
        )
        if receipt.declined:
            raise DuplicateCommitError(
                f"Cancellation refused by the ledger ({receipt.code.value}): {receipt.detail}",
                str(mission_id),
            )
        return cancelled_response(record)

    # ------------------------------------------------------------------ delegated reads
    def decision(self, principal: Principal, mission_id: uuid.UUID) -> DecisionTraceResponse:
        return self._or_double().decision(principal, mission_id)

    def uncertainty(self, principal: Principal, mission_id: uuid.UUID) -> UncertaintyStateResponse:
        return self._or_double().uncertainty(principal, mission_id)

    def observations(self, principal: Principal, mission_id: uuid.UUID) -> TelemetryListResponse:
        return self._or_double().observations(principal, mission_id)

    def preview_proposal(
        self, principal: Principal, mission_id: uuid.UUID, proposal_id: uuid.UUID, draft: ProposalDraft
    ) -> MissionResponse:
        return self._or_double().preview_proposal(principal, mission_id, proposal_id, draft)

    def accept_proposal(
        self, principal: Principal, mission_id: uuid.UUID, proposal_id: uuid.UUID, draft: ProposalDraft
    ) -> MissionResponse:
        return self._or_double().accept_proposal(principal, mission_id, proposal_id, draft)

    def decline_proposal(
        self, principal: Principal, mission_id: uuid.UUID, proposal_id: uuid.UUID, draft: ProposalDraft
    ) -> MissionResponse:
        return self._or_double().decline_proposal(principal, mission_id, proposal_id, draft)

    # ------------------------------------------------------------------ internals
    def _or_double(self) -> MissionPort:
        """The double, or a refusal that says which read model is missing.

        Kept as one small accessor so each delegated method stays typed: the delegation is
        a *named* call to the reference port, not a ``getattr`` that returns ``object``.
        """
        if self._fallback is None:
            raise ResourceNotFoundError("This read model is served by the reference plane")
        return self._fallback

    def _require(self, principal: Principal, mission_id: uuid.UUID) -> OrderRecord:
        record = self._plane.mission(mission_id)
        if record is None or record.tenant_id != principal.tenant_id:
            raise ResourceNotFoundError("Mission not found")
        return record

    def _note(self, principal: Principal, kind: str, mission_id: uuid.UUID, code: str, route: PlannedRoute) -> None:
        if self._audit is not None:
            self._audit(principal, kind, str(mission_id), code)

    @staticmethod
    def _response(record: OrderRecord, route: PlannedRoute) -> PlanVersionResponse:
        return PlanVersionResponse(
            plan_version_id=route.plan_version_id,
            mission_id=record.mission_id,
            route_hash=route.route_digest,
            energy_margin_wh=route.energy_margin_wh,
            config_release_sha=route.config_release_sha,
            created_at=route.created_at,
            strategy=route.strategy,
            resource_keys=[claim.identity for claim in route.claims],
            **provenance_fields(SourceKind.PLANNED),
        )


class LedgerReservationPort(ReservationPort):
    def __init__(self, plane: LedgerPlane) -> None:
        self._plane = plane

    def list(self, principal: Principal, time_from: datetime, time_to: datetime) -> ReservationListResponse:
        rows = self._plane.runner.run(
            self._plane.fetch_reservations(principal.tenant_id, time_from, time_to, None)
        )
        return ReservationListResponse(
            items=[reservation_response(principal.tenant_id, row) for row in rows],
            **provenance_fields(SourceKind.COMMITTED),
        )


class AuditChainPort(AuditPort):
    """``AuditPort`` over the real ``audit_log``, through the injected :class:`AuditSink`.

    One append is one INSERT whose ``prev_hash`` is that tenant's head, and the database's
    chain guard decides whether it is accepted. Nothing here holds a list, so a restart
    cannot lose an audit trail and a second process cannot fork the chain.
    """

    def __init__(self, sink: AuditSink, runner: LoopRunner, clock: Callable[[], datetime]) -> None:
        self._sink = sink
        self._runner = runner
        self._clock = clock

    def append(
        self,
        principal: Principal,
        *,
        operation_kind: str,
        resource_type: str,
        resource_id: str,
        reason_code: str,
        config_release_sha: str,
    ) -> AuditEventResponse:
        entry = AuditEntry(
            tenant_id=principal.tenant_id,
            occurred_at=self._clock(),
            actor_type=_actor_type(principal.actor_type),
            actor_id=principal.actor_id,
            action=operation_kind,
            resource_type=resource_type,
            resource_id=resource_id,
            request_id=principal.request_id,
            reason_code=reason_code,
            config_release_sha=config_release_sha,
        )
        record = self._runner.run(
            self._sink.append(context_of(principal, config_release_sha), entry)
        )
        return audit_response(record)

    def append_note(
        self, principal: Principal, operation_kind: str, resource_id: str, reason_code: str, sha: str
    ) -> None:
        self.append(
            principal,
            operation_kind=operation_kind,
            resource_type="mission",
            resource_id=resource_id,
            reason_code=reason_code,
            config_release_sha=sha,
        )

    def list(self, principal: Principal, time_from: datetime, time_to: datetime) -> AuditEventListResponse:
        records = self._runner.run(self._sink.window(context_of(principal, ""), time_from, time_to))
        return AuditEventListResponse(
            items=[audit_response(record) for record in records],
            **provenance_fields(SourceKind.COMMITTED),
        )

    def verify(self, tenant_id: uuid.UUID) -> Sequence[str]:
        """Re-derive the tenant's chain from the stored rows. Empty means intact.

        Named after what it returns, and typed as a sequence rather than ``list[str]``:
        this class also has an ``AuditPort`` method called ``list``, and an annotation that
        silently resolves to that method is a type error waiting for nobody to notice.
        """
        return self._runner.run(self._sink.verify(tenant_id))


# ------------------------------------------------------------------ value helpers
def context_of(principal: Principal, config_release_sha: str) -> TenantContext:
    return TenantContext(
        tenant_id=principal.tenant_id,
        actor=principal.actor_id,
        config_release_sha=config_release_sha,
        request_id=principal.request_id,
    )


def dedupe(claims: Sequence[ResourceClaim]) -> tuple[ResourceClaim, ...]:
    seen: dict[str, ResourceClaim] = {}
    for claim in claims:
        seen.setdefault(claim.identity, claim)
    return tuple(seen.values())


def commitment_token(mission_id: uuid.UUID, plan_version_id: uuid.UUID) -> str:
    return f"ct-{_digest('commitment', str(mission_id), str(plan_version_id))[:32]}"


def _track_profile(lane: str, start: datetime, stop: datetime) -> tuple[ProfileSample, ...]:
    (first_lat, first_lon), (second_lat, second_lon) = LANE_TRACKS[lane]
    return (
        ProfileSample(start, first_lat, first_lon, 60.0, AltitudeReference.AGL, 12_000),
        ProfileSample(stop, second_lat, second_lon, 60.0, AltitudeReference.AGL, 12_000),
    )


def _candidate(aircraft_id: str, priority_class: str) -> AircraftCandidate:
    return AircraftCandidate(
        aircraft_id=aircraft_id,
        status=AircraftStatus.AVAILABLE,
        link=LinkQuality.GOOD,
        priority_class=PriorityClass(priority_class if priority_class in set(PriorityClass) else "routine"),
        energy=EnergyVerdict(
            available_wh=200,
            consumed_wh=80,
            reserve_floor_wh=30,
            recovery_margin_wh=90,
            recovery_feasible=True,
            model_version="battery-curve-v1",
        ),
        lateness_us=0,
        disturbance_units=0,
        fairness_units=0,
    )


def _candidate_plan(route: PlannedRoute) -> CandidatePlan:
    return CandidatePlan(
        candidate_id=route.candidate_id,
        aircraft_id=route.aircraft_id,
        strategy=route.strategy,
        route_digest=route.route_digest,
        claims=route.claims,
        profile=route.profile,
        planner_verdict=Verdict.SAFE,
        config_release_sha=route.config_release_sha,
    )


def _actor_type(value: str) -> str:
    return value if value in _AUDIT_ACTOR_TYPES else "user"


def reservation_response(tenant_id: uuid.UUID, row: Mapping[str, object]) -> ReservationResponse:
    return ReservationResponse(
        reservation_id=uuid.UUID(str(row["reservation_id"])),
        tenant_id=tenant_id,
        mission_id=uuid.UUID(str(row["mission_id"])),
        resource_key=str(row["resource_key"]),
        resource_kind=cast(ResourceKindValue, str(row["resource_kind"])),
        valid_from=_instant(row["valid_from"]),
        valid_until=_instant(row["valid_until"]),
        state=cast(ReservationStateValue, str(row["state"])),
        commitment_token=str(row["commitment_token"] or ""),
        **provenance_fields(SourceKind.COMMITTED),
    )


def audit_response(record: AuditRecord) -> AuditEventResponse:
    return AuditEventResponse(
        audit_id=uuid.uuid5(uuid.NAMESPACE_OID, f"audit|{record.id}|{record.record_hash}"),
        tenant_id=record.tenant_id,
        occurred_at=record.entry.occurred_at,
        actor_type=record.entry.actor_type,
        actor_id=record.entry.actor_id,
        operation_kind=record.entry.action,
        resource_type=record.entry.resource_type,
        resource_id=record.entry.resource_id,
        request_id=record.entry.request_id,
        reason_code=record.entry.reason_code,
        config_release_sha=record.entry.config_release_sha,
        prev_hash=record.prev_hash,
        record_hash=record.record_hash,
        **provenance_fields(SourceKind.COMMITTED),
    )


def cancelled_response(record: OrderRecord) -> MissionResponse:
    return MissionResponse(
        mission_id=record.mission_id,
        tenant_id=record.tenant_id,
        order_id=record.order_id,
        status="cancelled",
        priority_class=record.priority_class,
        **provenance_fields(SourceKind.COMMITTED),
    )


def _instant(value: object) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    parsed = datetime.fromisoformat(str(value))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


__all__ = [
    "CELL_ADJACENCY",
    "CELL_BY_LANE",
    "LANE_ADJACENCY",
    "LANE_BY_DESTINATION",
    "LANE_TRACKS",
    "AuditChainPort",
    "AuditSink",
    "LedgerMissionPort",
    "LedgerOrderPort",
    "LedgerPlane",
    "LedgerReservationPort",
    "LoopRunner",
    "OrderRecord",
    "PlaneWorld",
    "PlannedRoute",
    "context_of",
    "dedupe",
    "frozen_adjacency",
    "lane_of",
    "protected_volume_json",
]
