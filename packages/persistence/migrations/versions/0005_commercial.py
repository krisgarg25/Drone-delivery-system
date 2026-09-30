"""Commercial: the order table and its idempotency guarantee.

Revision ID: 0005_commercial
Revises: 0004_network
Create Date: 2026-10-01

``UNIQUE (tenant_id, idempotency_key)`` is the whole of invariant I2 at the storage layer:
a partner that retries after a dropped connection produces a constraint violation, not a
second mission and not a second charge. The key is scoped by tenant so two partners reusing
the same string cannot collide (and one partner cannot probe another's keys, because
row-level security hides the row before the constraint can leak it).

``order`` is mutable because its status is a workflow state, but there is no DELETE policy:
cancelling is a transition, not a removal.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from droneops_persistence.models.commercial import Order
from droneops_persistence.schema_ddl import quote_identifier, secure_table

revision: str = "0005_commercial"
down_revision: str | None = "0004_network"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    Order.__table__.create(bind=bind)
    secure_table(bind, "order", mutable=True)
    # The unique key created with the table is named uq_order_tenant_id_idempotency_key by
    # the declarative naming convention. A test asserts it exists under exactly that name,
    # because "a duplicate insert is impossible" is only true while that index is present.


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql(f"DROP TABLE IF EXISTS {quote_identifier('order')} CASCADE")
