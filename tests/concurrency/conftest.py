"""Real-PostgreSQL fixtures for the reservation ledger proofs.

conventions section 11 and ``technical-build/06`` section 4: concurrency is proven
against the live PostGIS container, never against SQLite and never against a mock. The
DSN comes only from ``DRONEOPS_TEST_POSTGRES_DSN`` (published by the repository-root
``conftest.py`` from ``.env``); there is deliberately no default host and port, because a
guessed DSN would point destructive setup at whatever server happens to own 5432.

Every test gets its own tenant. Tenant scoping is what the ledger has to enforce anyway,
so a per-test tenant is both the isolation mechanism and part of the proof.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import asyncpg
import pytest
from ledger_support import World, make_ledger, seed_world

from droneops_conflict_checker import CheckerPolicy, load_policy_file
from droneops_reservations.ledger import LedgerStore, ReservationCommitLedger

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "packages" / "persistence" / "alembic.ini"
POLICY_PATH = REPO_ROOT / "contracts" / "configuration" / "protected_volume_policy.yaml"

BUSINESS_TABLES = (
    "outbox",
    "domain_event",
    "decision_record",
    "reservation",
    "plan_version",
    "mission",
    "order",
    "resource_catalog",
    "corridor",
    "config_release",
    "pad",
    "facility",
    "aircraft",
    "world_snapshot",
    "sim_run",
    "scenario",
    "tenant",
)

def dsn() -> str:
    value = os.environ.get("DRONEOPS_TEST_POSTGRES_DSN", "").strip()
    if not value:
        raise RuntimeError("DRONEOPS_TEST_POSTGRES_DSN is not set")
    return value


async def _prepare(value: str) -> str | None:
    """Migrate if needed and clear previous runs; ``None`` means unavailable."""
    try:
        connection = await asyncpg.connect(value, timeout=3)
    except (OSError, asyncpg.PostgresError):
        return None
    try:
        if await connection.fetchval("select to_regclass('public.reservation')") is None:
            env = dict(os.environ)
            env["DRONEOPS_DATABASE_URL"] = value
            alembic = Path(sys.executable).parent / ("alembic.exe" if os.name == "nt" else "alembic")
            result = subprocess.run(
                [str(alembic), "-c", str(ALEMBIC_INI), "upgrade", "head"],
                capture_output=True,
                text=True,
                check=False,
                env=env,
            )
            if result.returncode != 0:
                raise RuntimeError(result.stdout + result.stderr)
        await connection.execute(
            "truncate table "
            + ", ".join(f'"{name}"' for name in BUSINESS_TABLES)
            + " restart identity cascade"
        )
    finally:
        await connection.close()
    return value


@pytest.fixture(scope="session")
def postgres_dsn() -> str:
    try:
        value = dsn()
    except RuntimeError as error:
        pytest.skip(str(error))
    try:
        prepared = asyncio.run(_prepare(value))
    except (OSError, RuntimeError, asyncpg.PostgresError) as error:
        pytest.skip(f"PostGIS for ledger proofs is unavailable: {error}")
    if prepared is None:
        pytest.skip("PostGIS for ledger proofs is unreachable on the pinned DSN")
    return prepared


@pytest.fixture(scope="session")
def checker_policy() -> CheckerPolicy:
    if not POLICY_PATH.is_file():
        pytest.skip(f"no protected-volume policy at {POLICY_PATH}")
    return load_policy_file(POLICY_PATH)


async def _truncate(value: str) -> None:
    """Clear the business tables. ``resource_catalog`` is keyed on the *global* resource
    identity, so per-test tenant ids alone do not isolate seeding: each proof needs its own
    clean database, otherwise the second seed collides with the first."""
    connection = await asyncpg.connect(value, timeout=10)
    try:
        await connection.execute(
            "truncate table "
            + ", ".join(f'"{name}"' for name in BUSINESS_TABLES)
            + " restart identity cascade"
        )
    finally:
        await connection.close()


@pytest.fixture
async def store(postgres_dsn: str) -> AsyncIterator[LedgerStore]:
    await _truncate(postgres_dsn)
    ledger_store = LedgerStore(postgres_dsn, pool_max=24)
    await ledger_store.open()
    try:
        yield ledger_store
    finally:
        await ledger_store.close()




@pytest.fixture
async def world(store: LedgerStore, checker_policy: CheckerPolicy) -> World:
    return await seed_world(store, checker_policy)


@pytest.fixture
async def ledger(store: LedgerStore) -> ReservationCommitLedger:
    return make_ledger(store)
