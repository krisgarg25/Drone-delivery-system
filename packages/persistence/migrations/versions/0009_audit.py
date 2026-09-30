"""Audit: the hash-linked, append-only log.

Revision ID: 0009_audit
Revises: 0008_telemetry
Create Date: 2026-10-01

Spec section 4. Three layers keep this honest:

1. **grants** -- the application role gets ``INSERT`` and ``SELECT`` only;
2. **a trigger** -- ``audit_log_append_only`` refuses ``UPDATE``/``DELETE`` even for the
   table owner, so widening a grant later cannot turn the log into a mutable table;
3. **a chain guard** -- ``audit_log_chain_guard`` refuses a row whose ``prev_hash`` is not
   the ``record_hash`` of the tenant's newest row, taking a per-tenant advisory lock so two
   concurrent appenders serialise instead of forking the chain.

The canonical-payload rule for ``record_hash`` is documented once, on the table comment,
and implemented in ``droneops_persistence.audit``.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from droneops_persistence.audit import GENESIS_HASH
from droneops_persistence.models.audit import AuditLog
from droneops_persistence.schema_ddl import quote_identifier, quote_literal, secure_table

revision: str = "0009_audit"
down_revision: str | None = "0008_telemetry"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CANONICAL_RULE = (
    "record_hash = sha256(prev_hash || E'\\n' || canonical_json(row without id/prev_hash/"
    "record_hash)); canonical_json sorts object keys by code point, emits no insignificant "
    "whitespace, keeps UTF-8 unescaped, renders NULL as json null and instants as "
    "ISO-8601 UTC with microseconds and a literal Z; the first prev_hash in a tenant chain "
    "is 64 zeroes. Implemented in droneops_persistence.audit.canonical_json."
)

CHAIN_GUARD_SQL = """
CREATE OR REPLACE FUNCTION droneops_audit_chain_guard() RETURNS trigger
LANGUAGE plpgsql AS $fn$
DECLARE
    head_hash text;
BEGIN
    -- One chain per tenant; the advisory lock makes "the newest row is mine to extend"
    -- true even when two workers append in the same millisecond.
    PERFORM pg_advisory_xact_lock(hashtext(NEW.tenant_id::text));
    SELECT record_hash INTO head_hash
        FROM audit_log WHERE tenant_id = NEW.tenant_id ORDER BY id DESC LIMIT 1;
    IF head_hash IS NULL THEN
        IF NEW.prev_hash <> :genesis THEN
            RAISE EXCEPTION 'audit chain for tenant % starts at % not at genesis',
                NEW.tenant_id, NEW.prev_hash
                USING ERRCODE = 'data_exception';
        END IF;
    ELSIF NEW.prev_hash <> head_hash THEN
        RAISE EXCEPTION 'audit chain break for tenant %: prev_hash % does not extend %',
            NEW.tenant_id, NEW.prev_hash, head_hash
            USING ERRCODE = 'data_exception';
    END IF;
    RETURN NEW;
END;
$fn$;
""".replace(":genesis", quote_literal(GENESIS_HASH))


def upgrade() -> None:
    bind = op.get_bind()
    AuditLog.__table__.create(bind=bind)

    bind.exec_driver_sql(f"COMMENT ON TABLE audit_log IS {quote_literal(CANONICAL_RULE)}")
    for column in ("prev_hash", "record_hash"):
        bind.exec_driver_sql(
            f"COMMENT ON COLUMN audit_log.{column} IS {quote_literal(CANONICAL_RULE)}"
        )

    bind.exec_driver_sql(CHAIN_GUARD_SQL)
    bind.exec_driver_sql(
        "CREATE TRIGGER audit_log_chain_guard BEFORE INSERT ON audit_log "
        "FOR EACH ROW EXECUTE FUNCTION droneops_audit_chain_guard()"
    )

    secure_table(
        bind,
        "audit_log",
        append_only=True,
        grant_sequences=("audit_log_id_seq",),
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("DROP TRIGGER IF EXISTS audit_log_chain_guard ON audit_log")
    bind.exec_driver_sql("DROP FUNCTION IF EXISTS droneops_audit_chain_guard()")
    bind.exec_driver_sql(f"DROP TABLE IF EXISTS {quote_identifier('audit_log')} CASCADE")
