"""The database half of the ledger: connections, typed rows, one primitive per step.

Why raw SQL and not the ORM
    ``droneops_persistence`` owns the schema; this module owns a transaction that has
    to be *readable as a transaction*. ``SELECT ... FOR UPDATE`` acquired in a named
    order, ``xmin`` as a concurrency version and an all-or-nothing commit are each
    clearer as the statement they are. An ORM identity map would add a second,
    application-side notion of "what this row currently is" to the one piece of code
    where that notion is the entire safety argument.

Driver-type discipline
    Every parameter is passed as the Python type asyncpg infers for the column:
    ``UUID`` for ``uuid``, ``datetime`` for ``timestamptz``, ``str`` for ``text``,
    ``list[UUID]`` for ``uuid[]``. A string in a uuid position is not a style choice,
    it is a ``DataError`` at 03:00 under load. Values read back are converted at the
    boundary (:func:`_text`, :func:`_integer`, :func:`_instant`, :func:`_uuid`) so
    nothing downstream carries an untyped value - conventions section 10 forbids
    ``Any`` in a reservation API, and a driver record is exactly where one would leak.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from types import TracebackType
from typing import Self
from uuid import UUID

import asyncpg

from . import sql

Row = Mapping[str, object]


def _text(value: object) -> str:
    return value if isinstance(value, str) else str(value)


def _optional_text(value: object) -> str | None:
    return None if value is None else _text(value)


def _integer(value: object) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value)
    raise TypeError(f"expected an integer column, got {type(value).__name__}")


def _instant(value: object) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"expected a timestamptz column, got {type(value).__name__}")
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _uuid(value: object) -> UUID:
    return UUID(_text(value))


def _optional_uuid(value: object) -> UUID | None:
    return None if value is None else _uuid(value)


def _command_count(tag: str) -> int:
    """``"UPDATE 3"`` / ``"INSERT 0 1"`` -> the row count."""
    return int(tag.rsplit(" ", 1)[-1])


@dataclass(frozen=True)
class OrderRow:
    order_id: UUID
    status: str
    config_release_sha: str
    priority_class: str


@dataclass(frozen=True)
class MissionRow:
    mission_id: UUID
    status: str
    plan_version_id: UUID | None


@dataclass(frozen=True)
class ReservationRow:
    reservation_id: UUID
    mission_id: UUID
    resource_key: str
    state: str
    valid_from: datetime
    valid_until: datetime
    commitment_token: str | None = None
    profile_json: str = ""
    route_hash: str = ""
    config_release_sha: str = ""


@dataclass(frozen=True)
class ResourceRow:
    resource_key: str
    kind: str
    status: str
    capacity: int
    corridor_status: str = "open"


@dataclass(frozen=True)
class WorldRead:
    """What step 2 froze: the configuration release plus the resource rows touched."""

    config_release_id: UUID | None
    protected_volume_json: str
    resources: tuple[ResourceRow, ...]
    digest: str


@dataclass(frozen=True)
class RowCounts:
    reservations: int
    missions: int
    plans: int
    decisions: int
    events: int
    outbox: int

    @property
    def any(self) -> bool:
        return bool(
            self.reservations
            or self.missions
            or self.plans
            or self.decisions
            or self.events
            or self.outbox
        )


@dataclass(frozen=True)
class DecisionRow:
    decision_id: UUID
    input_hash: str
    reason_codes: tuple[str, ...]
    deciding_actor: str
    config_release_sha: str
    eligible_json: Mapping[str, object]
    rejected_json: Mapping[str, object]
    selected_plan_id: UUID | None


@dataclass(frozen=True)
class EventRow:
    event_id: UUID
    event_type: str
    event_seq: int
    payload_json: Mapping[str, object]


PROTECTED_STATE_LIST: tuple[str, ...] = ("committed", "activated", "unresolved")
"""The states that still hold space, spelled for ``= any($n::text[])``."""

COMMITTED_STATE_LIST: tuple[str, ...] = ("committed", "activated")
"""The states the database's exclusion constraint indexes."""


class Transaction:
    """One transaction, one tenant, UTC, with the tenant pinned before anything else."""

    def __init__(self, connection: asyncpg.Connection, tenant_id: UUID) -> None:
        self.connection = connection
        self.tenant_id = tenant_id
        self._transaction = connection.transaction()

    async def __aenter__(self) -> Self:
        # ``set_config`` takes text, not a uuid: asyncpg's parameter type follows the
        # function signature, so the tenant is passed in its string form deliberately.
        await self.connection.execute("select set_config('app.tenant_id', $1, true)", str(self.tenant_id))
        await self.connection.execute("select set_config('timezone', 'UTC', true)")
        await self._transaction.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        if exc_type is None:
            await self._transaction.commit()
        else:
            await self._transaction.rollback()
        return False

    async def fetch(self, statement: str, *args: object) -> tuple[Row, ...]:
        rows = await self.connection.fetch(statement, *args)
        return tuple(dict(row) for row in rows)

    async def fetchrow(self, statement: str, *args: object) -> Row | None:
        row = await self.connection.fetchrow(statement, *args)
        return None if row is None else dict(row)

    async def execute(self, statement: str, *args: object) -> int:
        return _command_count(await self.connection.execute(statement, *args))

    @asynccontextmanager
    async def savepoint(self) -> AsyncIterator[None]:
        """Undo step 9's rows without losing the locks or the outer transaction.

        asyncpg issues ``SAVEPOINT`` for a transaction started inside a transaction. The
        tentative reservations, the plan and the mission hops roll back here when the
        independent validator blocks promotion, while the resource locks stay held and the
        rejection record still commits - so a blocked vote leaves no operational row and a
        full explanation, which is exactly the pair the specification asks for.
        """
        nested = self.connection.transaction()
        await nested.start()
        try:
            yield
        except BaseException:
            await nested.rollback()
            raise
        await nested.commit()


class LedgerStore:
    """A pool plus one typed primitive per ledger step.

    Nothing in here decides anything. Every method reads, locks or writes exactly one
    thing and returns it, so that the decision logic stays in
    ``droneops_reservations`` and the ordering of calls stays in
    :class:`~droneops_reservations_ledger.service.ReservationCommitLedger`.
    """

    def __init__(self, dsn: str, *, pool_max: int = 8, statement_cache_size: int = 0) -> None:
        self._dsn = dsn
        self._pool_max = pool_max
        self._statement_cache_size = statement_cache_size
        self._pool: asyncpg.Pool | None = None

    async def open(self) -> None:
        if self._pool is None:
            self._pool = await asyncpg.create_pool(
                self._dsn,
                min_size=1,
                max_size=self._pool_max,
                statement_cache_size=self._statement_cache_size,
            )

    async def close(self) -> None:
        if self._pool is not None:
            await self.pool.close()
            self._pool = None

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("LedgerStore.open() was not awaited; there is no lazy pool")
        return self._pool

    @asynccontextmanager
    async def transaction(self, tenant_id: UUID) -> AsyncIterator[Transaction]:
        async with self.pool.acquire() as connection:
            async with Transaction(connection, tenant_id) as tx:
                yield tx

    async def raw(self) -> asyncpg.Connection:
        """A bare connection for tests that need to bypass the ledger deliberately."""
        return await self.pool.acquire()

    # ------------------------------------------------------------------ step 1
    async def lock_identity(self, tx: Transaction, token: str) -> None:
        await tx.fetchrow(sql.ADVISORY_LOCK, token)

    async def find_order(self, tx: Transaction, idempotency_key: str) -> OrderRow | None:
        row = await tx.fetchrow(sql.SELECT_ORDER, tx.tenant_id, idempotency_key)
        if row is None:
            return None
        return OrderRow(
            order_id=_uuid(row["row_id"]),
            status=_text(row["status"]),
            config_release_sha=_text(row["config_release_sha"]),
            priority_class=_text(row["priority_class"]),
        )

    async def find_mission_by_order(self, tx: Transaction, order_id: UUID) -> MissionRow | None:
        row = await tx.fetchrow(sql.SELECT_MISSION_BY_ORDER, tx.tenant_id, order_id)
        if row is None:
            return None
        return MissionRow(
            mission_id=_uuid(row["row_id"]),
            status=_text(row["status"]),
            plan_version_id=_optional_uuid(row["plan_version_id"]),
        )

    async def token_missions(self, tx: Transaction, token: str) -> tuple[UUID, ...]:
        rows = await tx.fetch(sql.SELECT_TOKEN_MISSIONS, tx.tenant_id, token)
        return tuple(_uuid(row["row_id"]) for row in rows)

    async def token_reservations(self, tx: Transaction, token: str) -> tuple[ReservationRow, ...]:
        rows = await tx.fetch(sql.SELECT_TOKEN_RESERVATIONS, tx.tenant_id, token)
        return tuple(_reservation(row) for row in rows)

    # ------------------------------------------------------------------ step 2
    async def read_world(
        self,
        tx: Transaction,
        identities: Sequence[str],
        config_release_sha: str,
        policy_version: str,
    ) -> WorldRead:
        release = await tx.fetchrow(sql.SELECT_CONFIG_RELEASE, tx.tenant_id, config_release_sha, policy_version)
        resources = tuple(
            ResourceRow(
                resource_key=_text(row["resource_key"]),
                kind=_text(row["kind"]),
                status=_text(row["status"]),
                capacity=_integer(row["capacity"]),
                corridor_status=_text(row["corridor_status"]),
            )
            for row in await tx.fetch(sql.SELECT_WORLD_RESOURCES, tx.tenant_id, list(identities))
        )
        frozen = json.dumps(
            {
                "config_release": None if release is None else _text(release["protected_volume"]),
                "release_sha": None if release is None else _text(release["config_release_sha"]),
                "resources": [
                    [r.resource_key, r.status, r.capacity, r.corridor_status] for r in resources
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return WorldRead(
            config_release_id=None if release is None else _uuid(release["row_id"]),
            protected_volume_json="" if release is None else _text(release["protected_volume"]),
            resources=resources,
            digest=hashlib.sha256(frozen.encode()).hexdigest(),
        )

    # ------------------------------------------------------------------ step 7
    async def lock_resources(self, tx: Transaction, identities: Sequence[str]) -> tuple[str, ...]:
        """Lock each identity's catalog row, in exactly the order given.

        This function deliberately does no sorting of its own: a second sort would be a
        second lock order, and two orders is precisely what a deadlock cycle needs.
        """
        locked: list[str] = []
        for identity in identities:
            row = await tx.fetchrow(sql.LOCK_RESOURCE, tx.tenant_id, identity)
            if row is None:
                raise LookupError(f"resource {identity} is not in the catalog for this tenant")
            locked.append(_text(row["resource_key"]))
        return tuple(locked)

    async def ensure_resource(
        self, tx: Transaction, identity: str, kind: str, source_release: str
    ) -> None:
        await tx.execute(sql.REGISTER_RESOURCE, identity, kind, tx.tenant_id, source_release, None)

    # ------------------------------------------------------------------ step 8
    async def claim_holders(
        self, tx: Transaction, kind: str, identifier: str, start: datetime, end: datetime
    ) -> tuple[ReservationRow, ...]:
        rows = await tx.fetch(
            sql.SELECT_CLAIM_HOLDERS,
            tx.tenant_id,
            list(PROTECTED_STATE_LIST),
            kind,
            identifier,
            start,
            end,
        )
        return tuple(_reservation(row) for row in rows)

    async def claim_version(
        self, tx: Transaction, kinds: Sequence[str], identifiers: Sequence[str]
    ) -> tuple[int, int]:
        row = await tx.fetchrow(
            sql.SELECT_CLAIM_VERSION,
            tx.tenant_id,
            list(PROTECTED_STATE_LIST),
            list(kinds),
            list(identifiers),
        )
        if row is None:
            return 0, 0
        return _integer(row["version"]), _integer(row["holder_count"])

    async def committed_overlapping(
        self, tx: Transaction, resource_key: str, start: datetime, end: datetime
    ) -> tuple[ReservationRow, ...]:
        rows = await tx.fetch(
            sql.SELECT_COMMITTED_OVERLAPPING,
            tx.tenant_id,
            list(PROTECTED_STATE_LIST),
            resource_key,
            start,
            end,
        )
        return tuple(_reservation(row) for row in rows)

    # ------------------------------------------------------------------ step 9
    async def insert_mission(
        self,
        tx: Transaction,
        mission_id: UUID,
        order_id: UUID,
        aircraft_id: UUID | None,
        status: str,
        plan_version_id: UUID | None,
        priority_class: str,
        policy_version: str,
    ) -> None:
        await tx.execute(
            sql.INSERT_MISSION,
            mission_id,
            order_id,
            tx.tenant_id,
            aircraft_id,
            status,
            plan_version_id,
            priority_class,
            policy_version,
        )

    async def update_mission_status(
        self, tx: Transaction, mission_id: UUID, from_status: str, to_status: str, plan_version_id: UUID
    ) -> int:
        return await tx.execute(
            sql.UPDATE_MISSION_STATUS, mission_id, tx.tenant_id, from_status, to_status, plan_version_id
        )

    async def insert_plan_version(
        self,
        tx: Transaction,
        plan_version_id: UUID,
        mission_id: UUID,
        route_hash: str,
        profile_json: str,
        energy_margin_wh: int,
        config_release_sha: str,
    ) -> None:
        await tx.execute(
            sql.INSERT_PLAN_VERSION,
            plan_version_id,
            mission_id,
            tx.tenant_id,
            route_hash,
            profile_json,
            energy_margin_wh,
            config_release_sha,
        )

    async def insert_reservation(
        self,
        tx: Transaction,
        reservation_id: UUID,
        mission_id: UUID,
        resource_key: str,
        resource_kind: str,
        valid_from: datetime,
        valid_until: datetime,
        commitment_token: str,
        config_release_sha: str,
    ) -> None:
        await tx.execute(
            sql.INSERT_RESERVATION,
            reservation_id,
            tx.tenant_id,
            mission_id,
            resource_key,
            resource_kind,
            valid_from,
            valid_until,
            commitment_token,
            config_release_sha,
        )

    async def insert_decision_record(
        self,
        tx: Transaction,
        decision_id: UUID,
        mission_id: UUID,
        input_hash: str,
        eligible_json: str,
        rejected_json: str,
        selected_plan_id: UUID | None,
        reason_codes: Sequence[str],
        deciding_actor: str,
        config_release_sha: str,
    ) -> None:
        await tx.execute(
            sql.INSERT_DECISION_RECORD,
            decision_id,
            mission_id,
            tx.tenant_id,
            input_hash,
            eligible_json,
            rejected_json,
            selected_plan_id,
            list(reason_codes),
            deciding_actor,
            config_release_sha,
        )

    async def promote(self, tx: Transaction, reservation_ids: Sequence[UUID]) -> int:
        return await tx.execute(sql.PROMOTE_RESERVATIONS, tx.tenant_id, list(reservation_ids))

    async def mark_unresolved(
        self, tx: Transaction, reservation_ids: Sequence[UUID], from_states: Sequence[str]
    ) -> int:
        return await tx.execute(
            sql.DEMOTE_TO_UNRESOLVED, tx.tenant_id, list(reservation_ids), list(from_states)
        )

    async def cancel(
        self, tx: Transaction, mission_id: UUID, reason_code: str, from_states: Sequence[str]
    ) -> int:
        return await tx.execute(
            sql.CANCEL_RESERVATIONS, tx.tenant_id, mission_id, reason_code, list(from_states)
        )

    async def mission_rows(self, tx: Transaction, mission_id: UUID) -> tuple[ReservationRow, ...]:
        rows = await tx.fetch(sql.SELECT_MISSION_ROWS, tx.tenant_id, mission_id)
        return tuple(_reservation(row) for row in rows)

    async def mission_status(self, tx: Transaction, mission_id: UUID) -> str | None:
        row = await tx.fetchrow(sql.SELECT_MISSION_STATUS, tx.tenant_id, mission_id)
        return None if row is None else _text(row["status"])

    async def cancel_mission(self, tx: Transaction, mission_id: UUID, from_states: Sequence[str]) -> int:
        """Move the mission itself to ``cancelled`` so a later commit sees the decision.

        The ``status = any(...)`` predicate is the guard: a mission that is already
        ``activated`` or terminal is not cancelled by this path, and the count the caller
        gets back is how it knows.
        """
        return await tx.execute(
            sql.CANCEL_MISSION, mission_id, tx.tenant_id, list(from_states)
        )

    # ------------------------------------------------------------------ step 10
    async def next_event_seq(self, tx: Transaction, stream_type: str, stream_id: UUID) -> int:
        row = await tx.fetchrow(sql.SELECT_NEXT_EVENT_SEQ, tx.tenant_id, stream_type, stream_id)
        return 1 if row is None else _integer(row["next_seq"])

    async def append_event(
        self,
        tx: Transaction,
        event_id: UUID,
        stream_type: str,
        stream_id: UUID,
        event_seq: int,
        event_type: str,
        payload_json: str,
        occurred_at: datetime,
        actor: str,
        config_release_sha: str,
        correlation_id: str,
        causation_id: str,
    ) -> None:
        await tx.execute(
            sql.INSERT_DOMAIN_EVENT,
            event_id,
            tx.tenant_id,
            stream_type,
            stream_id,
            event_seq,
            event_type,
            payload_json,
            occurred_at,
            occurred_at,
            actor,
            correlation_id,
            causation_id,
            config_release_sha,
        )
        await tx.execute(sql.INSERT_OUTBOX, event_id, tx.tenant_id)

    # ------------------------------------------------------------------ proofs
    async def policy_violations(self, tx: Transaction, states: Sequence[str]) -> tuple[Row, ...]:
        return await tx.fetch(sql.SELECT_POLICY_VIOLATIONS, list(states), tx.tenant_id)

    async def decisions(self, tx: Transaction, mission_id: UUID) -> tuple[DecisionRow, ...]:
        return tuple(
            DecisionRow(
                decision_id=_uuid(row["decision_id"]),
                input_hash=_text(row["input_hash"]),
                reason_codes=_codes(row["reason_codes"]),
                deciding_actor=_text(row["deciding_actor"]),
                config_release_sha=_text(row["config_release_sha"]),
                eligible_json=_mapping(row["eligible_json"]),
                rejected_json=_mapping(row["rejected_json"]),
                selected_plan_id=_optional_uuid(row["selected_plan_id"]),
            )
            for row in await tx.fetch(sql.SELECT_DECISION_FOR_MISSION, tx.tenant_id, mission_id)
        )

    async def events(self, tx: Transaction, mission_id: UUID) -> tuple[EventRow, ...]:
        return tuple(
            EventRow(
                event_id=_uuid(row["event_id"]),
                event_type=_text(row["event_type"]),
                event_seq=_integer(row["event_seq"]),
                payload_json=_mapping(row["payload_json"]),
            )
            for row in await tx.fetch(sql.SELECT_EVENTS_FOR_MISSION, tx.tenant_id, mission_id)
        )

    async def outbox_for(self, tx: Transaction, event_ids: Sequence[UUID]) -> tuple[tuple[UUID, bool], ...]:
        rows = await tx.fetch(sql.SELECT_OUTBOX_FOR_EVENTS, tx.tenant_id, list(event_ids))
        return tuple((_uuid(row["event_id"]), row["published_at"] is not None) for row in rows)

    async def counts_for(self, tx: Transaction, mission_ids: Sequence[UUID]) -> RowCounts:
        row = await tx.fetchrow(sql.COUNT_ROWS_FOR_MISSIONS, tx.tenant_id, list(mission_ids))
        if row is None:
            return RowCounts(0, 0, 0, 0, 0, 0)
        return RowCounts(
            reservations=_integer(row["reservations"]),
            missions=_integer(row["missions"]),
            plans=_integer(row["plans"]),
            decisions=_integer(row["decisions"]),
            events=_integer(row["events"]),
            outbox=_integer(row["outbox"]),
        )

    async def token_counts(self, tx: Transaction, token: str) -> tuple[int, int]:
        row = await tx.fetchrow(sql.COUNT_BY_COMMITMENT_TOKEN, tx.tenant_id, token)
        if row is None:
            return 0, 0
        return _integer(row["rows"]), _integer(row["missions"])


def _reservation(row: Row) -> ReservationRow:
    return ReservationRow(
        reservation_id=_uuid(row["reservation_id"]),
        mission_id=_uuid(row["mission_id"]),
        resource_key=_text(row["resource_key"]),
        state=_text(row["state"]),
        valid_from=_instant(row["valid_from"]),
        valid_until=_instant(row["valid_until"]),
        commitment_token=_optional_text(row.get("commitment_token")),
        profile_json=_text(row.get("profile_json") or ""),
        route_hash=_text(row.get("route_hash") or ""),
        config_release_sha=_text(row.get("config_release_sha") or ""),
    )


def _codes(value: object) -> tuple[str, ...]:
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return tuple(_text(item) for item in value)
    raise TypeError(f"expected a text[] column, got {type(value).__name__}")


def _mapping(value: object) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return dict(value)
    parsed = json.loads(_text(value))
    if isinstance(parsed, Mapping):
        return dict(parsed)
    raise TypeError(f"expected a JSON object column, got {type(value).__name__}")


__all__ = [
    "COMMITTED_STATE_LIST",
    "PROTECTED_STATE_LIST",
    "DecisionRow",
    "EventRow",
    "LedgerStore",
    "MissionRow",
    "OrderRow",
    "ResourceRow",
    "ReservationRow",
    "RowCounts",
    "Row",
    "Transaction",
    "WorldRead",
]
