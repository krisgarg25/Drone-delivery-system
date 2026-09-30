"""Identity: tenant, user_account, role, user_role, api_credential.

Revision ID: 0002_identity
Revises: 0001_baseline
Create Date: 2026-10-01

Spec section 2 "Tenant and access", section 12. The tables are created from the ORM
``Table`` objects rather than re-typed SQL, so the model and the schema cannot disagree;
the security half (RLS policy plus the application-role grant matrix) is applied per table
by :func:`secure_table`.

``tenant`` is the only table without a ``tenant_id`` column -- it *is* the tenant -- so its
policy is written against ``id``.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from droneops_persistence.models.identity import (
    ApiCredential,
    Role,
    Tenant,
    UserAccount,
    UserRole,
)
from droneops_persistence.schema_ddl import app_role, quote_identifier, secure_table

revision: str = "0002_identity"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = (Tenant, UserAccount, Role, UserRole, ApiCredential)


def _secure_tenant_table(bind: object) -> None:
    """``tenant`` keys its policy on ``id``, the one place that differs from the template."""

    bind.exec_driver_sql("ALTER TABLE tenant ENABLE ROW LEVEL SECURITY")
    bind.exec_driver_sql("ALTER TABLE tenant FORCE ROW LEVEL SECURITY")
    bind.exec_driver_sql(
        "CREATE POLICY tenant_self_read ON tenant FOR SELECT "
        "USING (id = droneops_current_tenant())"
    )
    # No INSERT policy for the application role: provisioning is an operator action, and
    # a runtime that could create tenants could create a second one and read its rows.
    bind.exec_driver_sql("REVOKE ALL ON tenant FROM PUBLIC")
    bind.exec_driver_sql(
        f"REVOKE ALL ON tenant FROM {quote_identifier(app_role())}"
    )
    # A runtime that could INSERT a tenant could read a second tenant's rows, so the
    # application role is a reader here and provisioning stays an operator action.
    bind.exec_driver_sql(
        f"GRANT SELECT ON tenant TO {quote_identifier(app_role())}"
    )


def upgrade() -> None:
    bind = op.get_bind()
    for model in TABLES:
        model.__table__.create(bind=bind)

    _secure_tenant_table(bind)
    secure_table(bind, "user_account", mutable=True)
    secure_table(bind, "role")
    secure_table(bind, "user_role", allow_delete=True)
    secure_table(bind, "api_credential", mutable=True)


def downgrade() -> None:
    bind = op.get_bind()
    # Reverse dependency order: credentials and grants before the accounts and roles they
    # point at, and the tenant last because every other row refers to it.
    for model in reversed(TABLES):
        bind.exec_driver_sql(
            f"DROP TABLE IF EXISTS {quote_identifier(model.__tablename__)} CASCADE"
        )
