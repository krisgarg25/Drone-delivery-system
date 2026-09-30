"""Reservation invariants on a real PostgreSQL, never a SQLite stand-in.

docs/conventions.md section 11: concurrency proofs run against real Postgres in Docker.
The database, not the application, is the thing that must refuse two committed
reservations over the same protected volume and a duplicate idempotency request.

Skipped when `make db-up` has not been run; CI always runs it against a PostGIS service.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import asyncpg
import pytest
from asyncpg.exceptions import ExclusionViolationError, UniqueViolationError

# No silent default: pointing destructive DDL at whatever server happens to own port 5432
# is not acceptable. CI and `make db-up` set this explicitly from .env.
DSN = os.environ.get("DRONEOPS_TEST_POSTGRES_DSN", "")
ALEMBIC_INI = Path("packages/persistence/alembic.ini")

pytestmark = pytest.mark.concurrency

EXCLUSION_DDL = """
CREATE EXTENSION IF NOT EXISTS btree_gist;
CREATE TEMP TABLE canary_reservation (
    id uuid PRIMARY KEY,
    resource_key text NOT NULL,
    valid_during tstzrange NOT NULL,
    state text NOT NULL CHECK (state IN ('tentative', 'committed', 'activated', 'cancelled'))
);
ALTER TABLE canary_reservation ADD CONSTRAINT canary_reservation_no_overlap
    EXCLUDE USING gist (
        resource_key WITH =,
        valid_during WITH &&
    ) WHERE (state IN ('committed', 'activated'));
"""

IDEMPOTENCY_DDL = """
CREATE TEMP TABLE canary_request (
    tenant_id text NOT NULL,
    idempotency_key text NOT NULL,
    mission_id uuid NOT NULL,
    PRIMARY KEY (tenant_id, idempotency_key)
);
"""


async def _connect() -> asyncpg.Connection:
    if not DSN:
        raise RuntimeError("DRONEOPS_TEST_POSTGRES_DSN is not set")
    return await asyncpg.connect(DSN, timeout=3)


@pytest.fixture
async def connection() -> AsyncIterator[asyncpg.Connection]:
    try:
        conn = await _connect()
    except (OSError, RuntimeError, asyncpg.PostgresError) as exc:
        pytest.skip(f"PostGIS for concurrency tests is unavailable: {exc}")
    try:
        yield conn
    finally:
        await conn.close()


async def test_committed_overlap_on_one_volume_is_rejected_by_the_database(
    connection: asyncpg.Connection,
) -> None:
    await connection.execute(EXCLUSION_DDL)
    resource = f"corridor:canary-{uuid.uuid4().hex[:8]}"
    await connection.execute(
        "INSERT INTO canary_reservation VALUES ($1, $2, tstzrange(now(), now() + interval '2 hours'), 'committed')",
        uuid.uuid4(),
        resource,
    )

    with pytest.raises(ExclusionViolationError):
        await connection.execute(
            "INSERT INTO canary_reservation VALUES ($1, $2, tstzrange(now(), now() + interval '1 hour'), 'committed')",
            uuid.uuid4(),
            resource,
        )

    # A cancelled reservation does not hold airspace, so a later window is permitted.
    await connection.execute(
        "INSERT INTO canary_reservation VALUES ($1, $2, tstzrange(now(), now() + interval '1 hour'), 'cancelled')",
        uuid.uuid4(),
        resource,
    )


async def test_separate_volumes_never_collide(connection: asyncpg.Connection) -> None:
    await connection.execute(EXCLUSION_DDL)
    window = "tstzrange(now(), now() + interval '2 hours')"
    await connection.execute(
        f"INSERT INTO canary_reservation VALUES ($1, 'corridor:a', {window}, 'committed')", uuid.uuid4()
    )
    await connection.execute(
        f"INSERT INTO canary_reservation VALUES ($1, 'corridor:b', {window}, 'committed')", uuid.uuid4()
    )
    assert await connection.fetchval("SELECT count(*) FROM canary_reservation") == 2


async def test_duplicate_request_is_rejected_but_a_second_tenant_is_not(
    connection: asyncpg.Connection,
) -> None:
    await connection.execute(IDEMPOTENCY_DDL)
    key = f"request-{uuid.uuid4().hex[:8]}"
    mission = uuid.uuid4()
    await connection.execute(
        "INSERT INTO canary_request VALUES ($1, $2, $3)", "tenant-a", key, mission
    )

    with pytest.raises(UniqueViolationError):
        await connection.execute("INSERT INTO canary_request VALUES ($1, $2, $3)", "tenant-a", key, uuid.uuid4())

    await connection.execute("INSERT INTO canary_request VALUES ($1, $2, $3)", "tenant-b", key, uuid.uuid4())
    assert await connection.fetchval("SELECT count(*) FROM canary_request") == 2


async def test_postgres_reports_the_safety_signals_ci_watches(connection: asyncpg.Connection) -> None:
    row = await connection.fetchrow(
        "SELECT current_setting('server_version') AS version, postgis_version() AS postgis"
    )
    assert row["version"].startswith("17")
    assert "3." in row["postgis"] or "4." in row["postgis"]


async def test_migrations_upgrade_and_downgrade(connection: asyncpg.Connection) -> None:
    if not ALEMBIC_INI.exists():
        pytest.skip("Alembic baseline is not committed yet (Phase 1 persistence task)")

    import subprocess
    import sys

    env = os.environ | {"DRONEOPS_DATABASE_URL": DSN}
    upgrade = subprocess.run(
        [str(Path(sys.executable).parent / ("alembic.exe" if os.name == "nt" else "alembic")),
         "-c", str(ALEMBIC_INI), "upgrade", "head"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert upgrade.returncode == 0, upgrade.stdout + upgrade.stderr
    downgrade = subprocess.run(
        [str(Path(sys.executable).parent / ("alembic.exe" if os.name == "nt" else "alembic")),
         "-c", str(ALEMBIC_INI), "downgrade", "base"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert downgrade.returncode == 0, downgrade.stdout + downgrade.stderr
