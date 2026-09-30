"""Schema DDL helpers shared by the migration revisions.

Row-level security, the append-only guard and the application-role grant matrix are
expressed here once. Every revision calls :func:`secure_table` for the tables it creates,
which is what makes "row-security policies versioned in migrations" (spec section 15) true
rather than aspirational -- and what stops a new table from being added without a policy,
because the helper raises when the table has no tenant column.

The two fail-closed properties that matter:

* ``droneops_current_tenant()`` returns NULL when ``app.tenant_id`` is unset, and
  ``tenant_id = NULL`` is never true, so an unauthenticated session reads nothing and
  writes nothing;
* the append-only guard is a ``RAISE`` trigger, not only a ``REVOKE``. Grants can be
  widened by a later migration and are not held by the table owner anyway; a trigger
  refuses the UPDATE even for the owner, so "committed operational records are
  append-only" (conventions section 5) is a property of the schema.
"""

from __future__ import annotations

import os
from collections.abc import Sequence

from sqlalchemy import Connection

APP_ROLE_ENV = "DRONEOPS_APP_DB_USER"
APP_ROLE_DEFAULT = "droneops_app"
APP_ROLE_PASSWORD_ENV = "DRONEOPS_APP_DB_PASSWORD"
#: Local-development password only. A deployed environment supplies its own secret; this
#: exists so `make migrate` works on a fresh clone without a manual psql step.
APP_ROLE_PASSWORD_DEFAULT = "local-development-only"

EXTENSIONS: tuple[str, ...] = ("postgis", "btree_gist", "pgcrypto")

FORBID_MUTATION_FUNCTION = "droneops_forbid_mutation"
CURRENT_TENANT_FUNCTION = "droneops_current_tenant"

# Dollar-quoted so the embedded single quotes in the RAISE message survive intact.
_CURRENT_TENANT_SQL = """
CREATE OR REPLACE FUNCTION droneops_current_tenant() RETURNS uuid
LANGUAGE sql STABLE AS $fn$
    SELECT nullif(current_setting('app.tenant_id', true), '')::uuid
$fn$;
"""

_FORBID_MUTATION_SQL = """
CREATE OR REPLACE FUNCTION droneops_forbid_mutation() RETURNS trigger
LANGUAGE plpgsql AS $fn$
BEGIN
    RAISE EXCEPTION 'append-only table % refuses %', TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'insufficient_privilege',
              HINT = 'Emit a superseding record instead of editing in place (docs/conventions.md section 5).';
END;
$fn$;
"""


def app_role() -> str:
    return os.environ.get(APP_ROLE_ENV, "").strip() or APP_ROLE_DEFAULT


def app_role_password() -> str:
    return os.environ.get(APP_ROLE_PASSWORD_ENV, "").strip() or APP_ROLE_PASSWORD_DEFAULT


def create_extensions(bind: Connection) -> None:
    """postgis for geometry, btree_gist so ``text WITH =`` can live in a GiST exclusion.

    ``btree_gist`` is the precondition of the spec's exclusion constraint: without it the
    ``EXCLUDE USING gist (resource_key WITH =, ...)`` on a text column cannot be planned.
    """

    for extension in EXTENSIONS:
        bind.exec_driver_sql(f"CREATE EXTENSION IF NOT EXISTS {quote_identifier(extension)}")


def create_helper_functions(bind: Connection) -> None:
    bind.exec_driver_sql(_CURRENT_TENANT_SQL)
    bind.exec_driver_sql(_FORBID_MUTATION_SQL)


def drop_helper_functions(bind: Connection) -> None:
    bind.exec_driver_sql(f"DROP FUNCTION IF EXISTS {CURRENT_TENANT_FUNCTION}()")
    bind.exec_driver_sql(f"DROP FUNCTION IF EXISTS {FORBID_MUTATION_FUNCTION}()")


def create_app_role(bind: Connection) -> None:
    """A non-superuser LOGIN role. Superusers bypass row-level security by definition, so
    the runtime must not be one."""

    role_name = app_role()
    quoted_role = quote_identifier(role_name)
    bind.exec_driver_sql(
        f"""
DO $do$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {quote_literal(role_name)}) THEN
        CREATE ROLE {quoted_role} LOGIN PASSWORD {quote_literal(app_role_password())}
            NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS NOREPLICATION;
    END IF;
END
$do$;
"""
    )
    # The UTC check constraints are only satisfiable by a UTC session; pinning the role
    # means a correct application connection is the default connection, not a lucky one.
    bind.exec_driver_sql(f"ALTER ROLE {quoted_role} SET timezone = 'UTC'")
    bind.exec_driver_sql(f"GRANT USAGE ON SCHEMA public TO {quoted_role}")


def drop_app_role(bind: Connection) -> None:
    quoted_role = quote_identifier(app_role())
    bind.exec_driver_sql(f"REVOKE ALL ON SCHEMA public FROM {quoted_role}")
    bind.exec_driver_sql(f"DROP ROLE IF EXISTS {quoted_role}")


def secure_table(
    bind: Connection,
    table: str,
    *,
    tenant_column: str = "tenant_id",
    mutable: bool = False,
    append_only: bool = False,
    allow_delete: bool = False,
    grant_sequences: Sequence[str] = (),
) -> None:
    """Enable RLS, install the tenant-isolation policy, set the application-role grants.

    ``mutable=True`` is for workflow state (mission status, reservation state, the outbox
    lease). It grants UPDATE only, never DELETE: dropping a business row is not a
    transition any state machine in this repository has.
    """

    if tenant_column not in _columns(bind, table):
        raise RuntimeError(
            f"{table} has no {tenant_column!r} column: every business table must be "
            "tenant-scoped so a row-level security policy can exist (conventions section 8)"
        )

    quoted_table = quote_identifier(table)
    quoted_role = quote_identifier(app_role())
    policy_target = f"{tenant_column} = {CURRENT_TENANT_FUNCTION}()"

    bind.exec_driver_sql(f"ALTER TABLE {quoted_table} ENABLE ROW LEVEL SECURITY")
    bind.exec_driver_sql(f"ALTER TABLE {quoted_table} FORCE ROW LEVEL SECURITY")
    bind.exec_driver_sql(
        f"CREATE POLICY {quote_identifier(table + '_tenant_read')} ON {quoted_table} "
        f"FOR SELECT USING ({policy_target})"
    )
    bind.exec_driver_sql(
        f"CREATE POLICY {quote_identifier(table + '_tenant_write')} ON {quoted_table} "
        f"FOR INSERT WITH CHECK ({policy_target})"
    )
    if mutable or allow_delete:
        bind.exec_driver_sql(
            f"CREATE POLICY {quote_identifier(table + '_tenant_update')} ON {quoted_table} "
            f"FOR UPDATE USING ({policy_target}) WITH CHECK ({policy_target})"
        )
    if allow_delete:
        bind.exec_driver_sql(
            f"CREATE POLICY {quote_identifier(table + '_tenant_delete')} ON {quoted_table} "
            f"FOR DELETE USING ({policy_target})"
        )

    # The grant matrix, spelled in the order that makes the intent auditable: strip
    # everything, add exactly SELECT/INSERT, then add UPDATE where the table is mutable.
    bind.exec_driver_sql(f"REVOKE ALL ON {quoted_table} FROM PUBLIC")
    bind.exec_driver_sql(f"REVOKE ALL ON {quoted_table} FROM {quoted_role}")
    bind.exec_driver_sql(f"GRANT SELECT, INSERT ON {quoted_table} TO {quoted_role}")
    if mutable or allow_delete:
        bind.exec_driver_sql(f"GRANT UPDATE ON {quoted_table} TO {quoted_role}")

    for sequence in grant_sequences:
        bind.exec_driver_sql(f"GRANT USAGE, SELECT ON SEQUENCE {quote_identifier(sequence)} TO {quoted_role}")

    if append_only:
        bind.exec_driver_sql(
            f"CREATE TRIGGER {quote_identifier(table + '_append_only')} "
            f"BEFORE UPDATE OR DELETE ON {quoted_table} "
            f"FOR EACH ROW EXECUTE FUNCTION {FORBID_MUTATION_FUNCTION}()"
        )


def spatial_index(bind: Connection, name: str, table: str, column: str) -> None:
    """Explicit GiST creation. Spec section 15 forbids leaving a spatial index implicit."""

    bind.exec_driver_sql(
        f"CREATE INDEX {quote_identifier(name)} ON {quote_identifier(table)} "
        f"USING gist ({quote_identifier(column)})"
    )


def drop_index(bind: Connection, name: str) -> None:
    bind.exec_driver_sql(f"DROP INDEX IF EXISTS {quote_identifier(name)}")


def quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _columns(bind: Connection, table: str) -> set[str]:
    rows = bind.exec_driver_sql(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = " + quote_literal(table)
    ).fetchall()
    return {row[0] for row in rows}


__all__ = [
    "APP_ROLE_DEFAULT",
    "EXTENSIONS",
    "app_role",
    "app_role_password",
    "create_app_role",
    "create_extensions",
    "create_helper_functions",
    "drop_app_role",
    "drop_helper_functions",
    "drop_index",
    "quote_identifier",
    "quote_literal",
    "secure_table",
    "spatial_index",
]
