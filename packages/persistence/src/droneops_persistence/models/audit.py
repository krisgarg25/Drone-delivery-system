"""Audit chain (migration group ``audit``).

Spec section 4. The chain is append-only and hash-linked: the application role has INSERT
and SELECT and nothing else, and a trigger refuses UPDATE/DELETE even for the table owner
so "append-only" is a property of the schema rather than a grant that a migration can
accidentally widen.

CANONICAL PAYLOAD RULE (the one comment the chain depends on):

``record_hash = sha256(prev_hash || "\\n" || canonical_json(audit row, excluding
id/prev_hash/record_hash))`` where ``canonical_json`` is RFC 8785-style: object keys sorted
lexically by code point, no insignificant whitespace, ``ensure_ascii=false`` UTF-8, NULL
rendered as JSON ``null``, timestamps rendered as ISO-8601 UTC with microsecond precision
and a literal ``Z``, byte-string fields rendered as lowercase hex. ``prev_hash`` for the
first record in a tenant chain is 64 zeroes. A verifier recomputes the same string from the
stored row alone -- no driver formatting, no locale, no dict order -- which is why the rule
lives in ``droneops_persistence.audit`` and nowhere else.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import uuid

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Identity,
    Index,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from ..declarative import Base
from ..types import TenantMixin, pg_enum, utc_instant_check

GENESIS_HASH = "0" * 64


class AuditLog(Base, TenantMixin):
    __tablename__ = "audit_log"
    __table_args__: tuple[Any, ...] = (
        utc_instant_check("occurred_at"),
        CheckConstraint("length(btrim(action)) > 0", name="action_is_not_blank"),
        CheckConstraint("length(btrim(resource_type)) > 0", name="resource_type_is_not_blank"),
        CheckConstraint(
            "before_sha256 is null or before_sha256 ~ '^[0-9a-f]{64}$'",
            name="before_sha256_is_hex",
        ),
        CheckConstraint(
            "after_sha256 is null or after_sha256 ~ '^[0-9a-f]{64}$'",
            name="after_sha256_is_hex",
        ),
        CheckConstraint("prev_hash ~ '^[0-9a-f]{64}$'", name="prev_hash_is_hex"),
        CheckConstraint("record_hash ~ '^[0-9a-f]{64}$'", name="record_hash_is_hex"),
        CheckConstraint(
            "config_release_sha ~ '^sha256:[0-9a-f]{64}$'",
            name="config_release_sha_is_canonical",
        ),
        # One record per hash: a replayed append is refused instead of silently duplicated.
        UniqueConstraint("record_hash", name="record_hash"),
        # The tenant index the RLS filter and the chain guard's per-tenant newest-row
        # lookup (`WHERE tenant_id = ? ORDER BY id DESC LIMIT 1`) both need, as one
        # composite: a separate `ix_audit_log_tenant_id` on `tenant_id` alone would carry
        # the same name and a second CREATE INDEX of it fails. The leftmost prefix of this
        # index serves plain tenant_id lookups, so nothing is lost.
        Index("ix_audit_log_tenant_id", "tenant_id", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False
    )
    occurred_at: Mapped[datetime] = mapped_column(nullable=False)
    actor_type: Mapped[str] = mapped_column(pg_enum("audit_actor_type"), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(200), nullable=False)
    action: Mapped[str] = mapped_column(String(200), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(200), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(200), nullable=False)
    before_sha256: Mapped[str | None] = mapped_column(String(64))
    after_sha256: Mapped[str | None] = mapped_column(String(64))
    request_id: Mapped[str | None] = mapped_column(String(200))
    # PII is tokenised: the log records that an IP/device participated, never which one.
    ip_hash: Mapped[str | None] = mapped_column(String(64))
    device_hash: Mapped[str | None] = mapped_column(String(64))
    reason_code: Mapped[str | None] = mapped_column(String(64))
    config_release_sha: Mapped[str] = mapped_column(String(71), nullable=False)
    prev_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default=text(f"'{GENESIS_HASH}'")
    )
    record_hash: Mapped[str] = mapped_column(String(64), nullable=False)
