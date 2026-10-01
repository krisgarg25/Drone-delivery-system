"""Repository test-module layout guards.

pytest runs in prepend import mode, which puts each test directory on ``sys.path``
by name. Two consequences are structural rather than stylistic, so they are enforced
here instead of left to review:

1. Test module basenames must be unique across the repository. ``packages/geom-core``
   and ``packages/energy`` both wanting a ``test_properties.py`` aborts collection
   repo-wide with an "import file mismatch" - a failure that looks unrelated to the
   package that caused it.
2. A test module must not ``from conftest import ...``. ``conftest`` is one flat
   module name; whichever package is imported first wins, so an adapter silently
   received the concurrency fixtures and failed to import. Shared builders belong in a
   ``*_support.py`` module that is imported by its real name.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

SKIP_DIRS = frozenset({".venv", "node_modules", "__pycache__", ".git", "dist", "build"})

_CONTFEST_IMPORT = re.compile(r"^\s*(?:from|import)\s+conftest\b", re.MULTILINE)


def _walk_test_files() -> list[Path]:
    found: list[Path] = []
    for path in REPO_ROOT.rglob("test_*.py"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        found.append(path)
    return sorted(found)


def test_module_basenames_are_unique_repo_wide() -> None:
    files = _walk_test_files()
    assert files, "no test files discovered - the guard itself is broken"
    duplicates = {name: count for name, count in Counter(f.name for f in files).items() if count > 1}
    offenders = {name: sorted(str(f.relative_to(REPO_ROOT)) for f in files if f.name == name) for name in duplicates}
    assert not offenders, f"duplicate test module basenames break collection: {offenders}"


def test_no_test_module_imports_from_conftest_by_flat_name() -> None:
    offenders: list[str] = []
    for path in _walk_test_files():
        if _CONTFEST_IMPORT.search(path.read_text(encoding="utf-8")):
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert not offenders, (
        f"import shared builders from a named *_support.py module, not conftest: {offenders}"
    )
