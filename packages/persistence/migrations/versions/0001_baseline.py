"""Baseline: extensions, enums, the application role and the RLS helper functions.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-10-01

Every later revision assumes this one. It is deliberately the only revision that touches
cluster-level objects, so "what does this schema need to exist" has one answer.

* ``postgis`` -- geometry with a pinned CRS.
* ``btree_gist`` -- without it the spec's ``EXCLUDE USING gist (resource_key WITH =, ...)``
  on a text column cannot be built, so the reservation exclusion has no meaning here.
* ``pgcrypto`` -- digest() for anchors and hashing checks the verifier can do inside SQL.
* The closed enum label sets, created once so no table can invent a state.
* ``droneops_app``, a non-superuser LOGIN role: a superuser bypasses row-level security by
  definition, so the runtime must never be one.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from droneops_persistence.schema_ddl import (
    create_app_role,
    create_extensions,
    create_helper_functions,
    drop_app_role,
    drop_helper_functions,
    quote_identifier,
    quote_literal,
)
from droneops_persistence.types import ENUM_TYPES

revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Dropped on the way down. ``postgis`` is not in this list: the image's ``tiger`` and
#: ``topology`` schemas depend on it, so dropping it is destructive to objects this
#: migration does not own -- exactly the "not safe" case spec section 15 allows to stay.
_REVERSIBLE_EXTENSIONS: tuple[str, ...] = ("btree_gist", "pgcrypto")


def _create_enum(name: str, values: Sequence[str]) -> None:
    bind = op.get_bind()
    labels = ", ".join(quote_literal(value) for value in values)
    bind.exec_driver_sql(
        f"""
DO $do$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
                   WHERE t.typname = {quote_literal(name)} AND n.nspname = 'public') THEN
        CREATE TYPE {quote_identifier(name)} AS ENUM ({labels});
    END IF;
END
$do$;
"""
    )


def upgrade() -> None:
    create_extensions(op.get_bind())
    for name, values in ENUM_TYPES.items():
        _create_enum(name, values)
    create_app_role(op.get_bind())
    create_helper_functions(op.get_bind())


def downgrade() -> None:
    bind = op.get_bind()
    drop_helper_functions(bind)
    drop_app_role(bind)
    for name in ENUM_TYPES:
        bind.exec_driver_sql(f"DROP TYPE IF EXISTS {quote_identifier(name)} CASCADE")
    for extension in _REVERSIBLE_EXTENSIONS:
        bind.exec_driver_sql(f"DROP EXTENSION IF EXISTS {quote_identifier(extension)}")
