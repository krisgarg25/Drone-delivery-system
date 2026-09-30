"""The boundary configuration must bite, not just exist.

Every safety story in this repository rests on `import-linter` refusing a build. A config
that silently stopped matching module names is the real failure mode, so these tests
write a deliberate violation into the working tree, run `lint-imports` and assert that the
specific contract reports BROKEN. The synthetic file is always removed again.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

VIOLATIONS: list[tuple[str, str, str, str]] = [
    (
        "apps/api/src/droneops_api",
        "_importlinter_canary.py",
        "from droneops_gateway import nothing  # deliberate canary violation\n",
        "API cannot reach a vehicle command",
    ),
    (
        "packages/energy/src/droneops_energy",
        "_importlinter_canary.py",
        "import sqlalchemy  # deliberate canary violation\n",
        "Energy model has no database",
    ),
    (
        "packages/sim-core/src/droneops_sim_core",
        "_importlinter_canary.py",
        "import droneops_persistence  # deliberate canary violation\n",
        "Simulation kernel has no database",
    ),
    (
        "packages/conflict-checker/src/droneops_conflict_checker",
        "_importlinter_canary.py",
        "import droneops_planner  # deliberate canary violation\n",
        "Conflict checker is independent",
    ),
]


def lint_imports_command() -> list[str]:
    scripts = Path(sys_executable_dir())
    name = "lint-imports.exe" if os.name == "nt" else "lint-imports"
    return [str(scripts / name), "--no-cache"]


def sys_executable_dir() -> str:
    import sys

    return str(Path(sys.executable).parent)


@pytest.fixture
def clean_import_linter_cache() -> Iterator[None]:
    yield
    cache = REPO_ROOT / ".import_linter_cache"
    for path in sorted(cache.glob("*.data.json")):
        path.unlink(missing_ok=True)


@pytest.mark.security
@pytest.mark.parametrize(
    ("relative_dir", "file_name", "source", "expected_contract"),
    VIOLATIONS,
    ids=[violation[3].split()[0].lower() for violation in VIOLATIONS],
)
def test_lint_imports_fails_on_synthetic_violation(
    relative_dir: str,
    file_name: str,
    source: str,
    expected_contract: str,
    clean_import_linter_cache: None,
) -> None:
    target = REPO_ROOT / relative_dir / file_name
    assert not target.exists(), f"a leftover canary file would poison the tree: {target}"
    target.write_text(source, encoding="utf-8")
    try:
        result = subprocess.run(
            lint_imports_command(),
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        target.unlink(missing_ok=True)

    output = result.stdout + result.stderr
    assert result.returncode != 0, (
        "import-linter accepted a deliberate forbidden import; the safety boundary is "
        f"not being enforced. Output:\n{output}"
    )
    assert "BROKEN" in output, output
    assert expected_contract in output, f"{expected_contract} did not report the violation:\n{output}"


@pytest.mark.security
def test_lint_imports_passes_on_a_clean_tree(clean_import_linter_cache: None) -> None:
    result = subprocess.run(
        lint_imports_command(),
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "BROKEN" not in result.stdout
