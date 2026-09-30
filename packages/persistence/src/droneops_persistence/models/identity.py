"""Identity and access tables (migration group ``identity``).

Spec section 2 "Tenant and access". PII is minimised at the column level: an account
stores a SHA-256 of the address, never the address (spec section 4 "PII is minimized and
hashed or tokenized when direct identifiers are unnecessary"), and a credential stores
nothing about the secret itself.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    ARRAY,
    CheckConstraint,
    ForeignKey,
    Index,
    String,
    Text,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ..declarative import Base
from ..types import pg_enum, tenant_index, TenantMixin

#: The closed Phase 1 role set from spec section 12. There is deliberately no
#: ``aircraft_commander`` value, so no row can ever name one.
PHASE1_ROLES: tuple[str, ...] = (
    "partner_order_writer",
    "scheduler_operator",
    "pilot_observer",
    "safety_reviewer",
    "auditor",
    "platform_admin",
)


class Tenant(Base):
    __tablename__ = "tenant"
    __table_args__: tuple[Any, ...] = (
        CheckConstraint("length(btrim(name)) > 0", name="name_is_not_blank"),
        CheckConstraint(
            "data_region = any (array['in-central','in-west','unknown'])",
            name="data_region_is_known",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(
        pg_enum("tenant_status"), nullable=False, server_default=text("'provisioning'")
    )
    data_region: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=text("now()")
    )


class UserAccount(Base, TenantMixin):
    __tablename__ = "user_account"
    __table_args__: tuple[Any, ...] = (
        tenant_index("user_account"),
        CheckConstraint(
            "email_hash ~ '^[0-9a-f]{64}$'", name="email_hash_is_sha256_hex"
        ),
        Index("ix_user_account_email_hash", "email_hash"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tenant.id", ondelete="RESTRICT"),
        nullable=False,
    )
    email_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        pg_enum("account_status"), nullable=False, server_default=text("'invited'")
    )
    created_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=text("now()")
    )


class Role(Base, TenantMixin):
    __tablename__ = "role"
    __table_args__: tuple[Any, ...] = (
        tenant_index("role"),
        CheckConstraint("length(btrim(name)) > 0", name="name_is_not_blank"),
        CheckConstraint(
            "name = any (array[" + ", ".join(f"'{r}'" for r in PHASE1_ROLES) + "])",
            name="name_is_a_phase1_role",
        ),
        CheckConstraint("coalesce(array_length(capability_scopes, 1), 0) > 0", name="has_scopes"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    capability_scopes: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'")
    )


class UserRole(Base, TenantMixin):
    __tablename__ = "user_role"
    __table_args__: tuple[Any, ...] = (
        tenant_index("user_role"),
        CheckConstraint("length(btrim(granted_by)) > 0", name="granted_by_is_not_blank"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("user_account.id", ondelete="CASCADE"), primary_key=True
    )
    role_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("role.id", ondelete="RESTRICT"), primary_key=True
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False
    )
    granted_by: Mapped[str] = mapped_column(String(200), nullable=False)
    granted_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))


class ApiCredential(Base, TenantMixin):
    __tablename__ = "api_credential"
    __table_args__: tuple[Any, ...] = (
        tenant_index("api_credential"),
        CheckConstraint("length(btrim(name)) > 0", name="name_is_not_blank"),
        CheckConstraint("coalesce(array_length(scopes, 1), 0) > 0", name="has_scopes"),
        CheckConstraint(
            "expires_at is null or expires_at > created_at",
            name="expiry_is_after_creation",
        ),
        CheckConstraint("expires_at is null or extract(timezone from expires_at) = 0", name="expires_at_is_utc"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    read_only: Mapped[bool] = mapped_column(nullable=False, server_default=text("true"))
    status: Mapped[str] = mapped_column(
        pg_enum("credential_status"), nullable=False, server_default=text("'active'")
    )
    # Only ever a hash of the credential; the secret is not storable or readable back.
    secret_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))
    expires_at: Mapped[datetime | None] = mapped_column()
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )


__all__ = [
    "PHASE1_ROLES",
    "ApiCredential",
    "Role",
    "Tenant",
    "TenantMixin",
    "UserAccount",
    "UserRole",
]
