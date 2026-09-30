"""Pytest bootstrap for the monorepo.

Makes the repository root importable so tests can exercise the CI scripts in `scripts/`
exactly the way the workflow runs them, and keeps the deterministic rules visible: a test
may not rely on the wall clock or on an unseeded random source.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Every test run is a simulation run: no live data source may be reachable.
os.environ.setdefault("DRONEOPS_SIMULATION_ONLY", "1")


def _load_local_env() -> None:
    """Publish the git-ignored local `.env` so DB-backed tests can find the DSN.

    `tests/concurrency` refuses to guess a server: it only runs when
    ``DRONEOPS_TEST_POSTGRES_DSN`` names the pinned local PostGIS container. Reading the
    file here keeps `make db-up` + `make test` a two-command workflow while leaving the
    "no silent default" rule intact — with no `.env` and no exported variable the tests
    still skip rather than attack whatever owns port 5432.
    """
    env_file = REPO_ROOT / ".env"
    if not env_file.exists():
        return
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - dotenv ships with uvicorn[standard]
        return
    load_dotenv(env_file, override=False)


_load_local_env()

