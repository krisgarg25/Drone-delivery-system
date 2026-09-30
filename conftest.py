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
