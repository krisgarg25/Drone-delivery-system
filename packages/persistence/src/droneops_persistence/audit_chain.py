"""The real ``audit_log``: a hash-chained, append-only sink over asyncpg.

This is the adapter the specification describes and the API has been missing: every
``append`` is one ``INSERT`` into ``audit_log`` whose ``prev_hash`` is the ``record_hash``
of that tenant's newest row, computed by :func:`droneops_persistence.audit.chain_hash`
over the canonical payload - no second implementation of the rule, because a second
implementation is a second answer.

Three things make the log trustworthy, and this module deliberately relies on all three
rather than re-enforcing them in Python:

``audit_log_append_only`` (migration 0009)
    ``BEFORE UPDATE OR DELETE``, and it raises even for the table owner. Append-only is a
    property of the schema, not of this class's discipline.
``audit_log_chain_guard`` (migration 0009)
    ``BEFORE INSERT``: it refuses a ``prev_hash`` that does not extend the tenant's head,
    and it takes ``pg_advisory_xact_lock(hashtext(tenant_id))`` while doing so.
``droneops_app`` grants
    ``SELECT, INSERT`` only - ``REVOKE ALL`` first, so widening a later migration cannot
    silently hand the application role a mutable log.

Because the guard's advisory lock is *per tenant* and is an ``xact`` lock, a correct
appender must take the same key **before** it reads the head it intends to extend. Reading
first and inserting second would let two concurrent appends compute the same ``prev_hash``,
after which one of them is refused by the guard instead of simply waiting. ``append``
therefore opens the transaction, pins the tenant, takes the tenant's advisory lock, reads
the head, hashes, and inserts - which is what turns a race into a queue.

Row-level security is ``FORCE``d on the table, so the tenant is pinned with
``set_config('app.tenant_id', ...)`` exactly as the reservation ledger pins it; the
explicit ``tenant_id`` predicate stays in every statement anyway, because a policy that is
only ever exercised through one helper function is a policy nobody can review.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Final
from uuid import UUID

import asyncpg

from droneops_platform.ports import AuditEntry, AuditRecord, TenantContext

from .audit import GENESIS_HASH, ChainLink, chain_hash, verify_chain

#: Every column that enters ``record_hash``, in the order the canonical mapping is built.
#: ``id``, ``prev_hash`` and ``record_hash`` are excluded by rule, not by omission here:
#: :data:`droneops_persistence.audit.EXCLUDED_FIELDS` is what drops them.
HASHED_COLUMNS: Final[tuple[str, ...]] = (
    "tenant_id",
    "occurred_at",
    "actor_type",
    "actor_id",
    "action",
    "resource_type",
    "resource_id",
    "before_sha256",
    "after_sha256",
    "request_id",
    "ip_hash",
    "device_hash",
    "reason_code",
    "config_release_sha",
)

_READ_COLUMNS: Final[str] = ", ".join(HASHED_COLUMNS) + ", id, prev_hash, record_hash"

ADVISORY_LOCK: Final[str] = "select pg_advisory_xact_lock(hashtext($1::text))"

SELECT_HEAD: Final[str] = """
select record_hash
  from audit_log
 where tenant_id = $1
 order by id desc
 limit 1
"""

SELECT_NEWEST: Final[str] = f"""
select {_READ_COLUMNS}
  from audit_log
 where tenant_id = $1
 order by id desc
 limit 1
"""

SELECT_CHAIN: Final[str] = f"""
select {_READ_COLUMNS}
  from audit_log
 where tenant_id = $1
 order by id
"""

SELECT_WINDOW: Final[str] = f"""
select {_READ_COLUMNS}
  from audit_log
 where tenant_id = $1
   and occurred_at >= $2
   and occurred_at < $3
 order by id
"""

INSERT_RECORD: Final[str] = """
insert into audit_log (tenant_id, occurred_at, actor_type, actor_id, action, resource_type,
                       resource_id, before_sha256, after_sha256, request_id, ip_hash,
                       device_hash, reason_code, config_release_sha, prev_hash, record_hash)
values ($1, $2, $3::audit_actor_type, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16)
returning id
"""


class AuditChainUnavailable(RuntimeError):
    """The log could not be reached or written; never silently a missing record."""


def hashed_fields(row: Mapping[str, object]) -> dict[str, object]:
    """The canonical payload of a stored or pending row.

    One function builds it for the writer and for the verifier, so the two can never
    drift into agreement by accident: the verifier recomputes from the row, and the row is
    the only evidence.
    """
    fields: dict[str, object] = {}
    for column in HASHED_COLUMNS:
        value = row.get(column)
        if isinstance(value, str) and value == "" and column in {"ip_hash", "device_hash"}:
            value = None
        if isinstance(value, datetime) and value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        fields[column] = value
    return fields


def record_hash_for(prev_hash: str, fields: Mapping[str, object]) -> str:
    return chain_hash(prev_hash, hashed_fields(fields))


class PostgresAuditChain:
    """Append, read and verify the hash chain of one PostgreSQL ``audit_log``.

    Async-native by construction: it owns an ``asyncpg`` pool and nothing else. The
    synchronous control plane reaches it through a loop runner supplied by the
    composition root, so the bridging policy stays in one place instead of being
    re-invented per call site.
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
            await self._pool.close()
            self._pool = None

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise AuditChainUnavailable("PostgresAuditChain.open() was not awaited")
        return self._pool

    # ------------------------------------------------------------------ append
    async def append(self, context: TenantContext, entry: AuditEntry) -> AuditRecord:
        """One append, one transaction, one link in that tenant's chain."""
        if context.tenant_id != entry.tenant_id:
            raise AuditChainUnavailable(
                f"tenant context {context.tenant_id} does not match entry {entry.tenant_id}"
            )
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    "select set_config('app.tenant_id', $1, true)", str(entry.tenant_id)
                )
                await connection.execute(
                    "select set_config('timezone', 'UTC', true)"
                )
                # Same key the chain guard takes, taken *before* the head read: a
                # concurrent appender queues here instead of racing it to the same
                # prev_hash.
                await connection.execute(ADVISORY_LOCK, str(entry.tenant_id))
                head = await connection.fetchval(SELECT_HEAD, entry.tenant_id)
                prev_hash = GENESIS_HASH if head is None else str(head)
                payload = self._payload(entry, prev_hash)
                record_hash = chain_hash(prev_hash, hashed_fields(payload))
                try:
                    row_id = await connection.fetchval(INSERT_RECORD, *payload.values(), record_hash)
                except asyncpg.PostgresError as error:  # pragma: no cover - defensive
                    raise AuditChainUnavailable(
                        f"audit append refused by the database: {error}"
                    ) from error
                return AuditRecord(
                    id=int(row_id),
                    tenant_id=entry.tenant_id,
                    prev_hash=prev_hash,
                    record_hash=record_hash,
                    entry=entry,
                )

    @staticmethod
    def _payload(entry: AuditEntry, prev_hash: str) -> dict[str, object]:
        """The insert values in column order, with blanks stored as absent.

        ``ip_hash``/``device_hash`` arrive as ``""`` from a caller that saw no client
        address; an empty string is not a hash, and ``null`` is what "not recorded" means
        in a chain a third party has to re-derive.
        """
        return {
            "tenant_id": entry.tenant_id,
            "occurred_at": entry.occurred_at,
            "actor_type": entry.actor_type,
            "actor_id": entry.actor_id,
            "action": entry.action,
            "resource_type": entry.resource_type,
            "resource_id": entry.resource_id,
            "before_sha256": entry.before_sha256,
            "after_sha256": entry.after_sha256,
            "request_id": entry.request_id or None,
            "ip_hash": entry.ip_hash or None,
            "device_hash": entry.device_hash or None,
            "reason_code": entry.reason_code or None,
            "config_release_sha": entry.config_release_sha,
            "prev_hash": prev_hash,
        }

    # ------------------------------------------------------------------ reads
    async def head(self, context: TenantContext) -> AuditRecord | None:
        async with self.pool.acquire() as connection:
            row = await connection.fetchrow(SELECT_NEWEST, context.tenant_id)
        return None if row is None else _record(dict(row))

    async def chain(self, context: TenantContext) -> tuple[AuditRecord, ...]:
        async with self.pool.acquire() as connection:
            rows = await connection.fetch(SELECT_CHAIN, context.tenant_id)
        return tuple(_record(dict(row)) for row in rows)

    async def window(
        self, context: TenantContext, time_from: datetime, time_to: datetime
    ) -> tuple[AuditRecord, ...]:
        async with self.pool.acquire() as connection:
            rows = await connection.fetch(SELECT_WINDOW, context.tenant_id, time_from, time_to)
        return tuple(_record(dict(row)) for row in rows)

    async def verify(self, tenant_id: UUID) -> list[str]:
        """Recompute the whole tenant chain; an empty list means it is intact.

        Read from the stored rows only - if the verifier needed the writer's memory, a
        rewritten row would verify.
        """
        async with self.pool.acquire() as connection:
            rows = await connection.fetch(SELECT_CHAIN, tenant_id)
        links = [
            ChainLink(
                ordinal=index,
                tenant_id=tenant_id,
                prev_hash=str(row["prev_hash"]),
                record_hash=str(row["record_hash"]),
                fields=hashed_fields(dict(row)),
            )
            for index, row in enumerate(rows, start=1)
        ]
        return verify_chain(links)


def _record(row: dict[str, object]) -> AuditRecord:
    return AuditRecord(
        id=int(str(row["id"])),
        tenant_id=UUID(str(row["tenant_id"])),
        prev_hash=str(row["prev_hash"]),
        record_hash=str(row["record_hash"]),
        entry=AuditEntry(
            tenant_id=UUID(str(row["tenant_id"])),
            occurred_at=_instant(row["occurred_at"]),
            actor_type=str(row["actor_type"]),
            actor_id=str(row["actor_id"]),
            action=str(row["action"]),
            resource_type=str(row["resource_type"]),
            resource_id=str(row["resource_id"]),
            request_id="" if row["request_id"] is None else str(row["request_id"]),
            reason_code="" if row["reason_code"] is None else str(row["reason_code"]),
            config_release_sha=str(row["config_release_sha"]),
            before_sha256=None if row["before_sha256"] is None else str(row["before_sha256"]),
            after_sha256=None if row["after_sha256"] is None else str(row["after_sha256"]),
            ip_hash="" if row["ip_hash"] is None else str(row["ip_hash"]),
            device_hash="" if row["device_hash"] is None else str(row["device_hash"]),
        ),
    )


def _instant(value: object) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    parsed = datetime.fromisoformat(str(value))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


__all__ = [
    "ADVISORY_LOCK",
    "GENESIS_HASH",
    "HASHED_COLUMNS",
    "AuditChainUnavailable",
    "PostgresAuditChain",
    "hashed_fields",
    "record_hash_for",
]
