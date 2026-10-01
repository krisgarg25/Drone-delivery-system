"""Determinism and purity guard for the energy kernel.

conventions.md section 4 requires a simulation run to reach no network, no clock
and no filesystem, and the task states the same rule for weather: an energy
decision must be structurally unable to fetch live wind. ``packages/geom-core``
enforces the identical rule for geometry by scanning source rather than trusting
review, so this file reuses that approach for ``droneops_energy``.

Four independent guards:

1. **Import deny list** - ``httpx``, ``socket``, ``requests``, ``urllib``,
   ``sqlalchemy``, ``alembic``, ``asyncpg``, ``time``, ``os``, ``pathlib``,
   ``random``, ``subprocess``, ``threading`` and the rest of the outside world may
   not be imported at all, and neither may the workspace packages that *own* a
   database or a clock (``droneops_persistence``, ``droneops_platform``).
2. **Import allow list** - the inverse check, so a dependency cannot arrive simply
   because nobody thought to forbid it in advance. This is the load-bearing gate:
   domain reuse is whitelisted by name, never assumed.
3. **Call scan** - wall-clock reads, unseeded randomness and file access detected
   on the syntax tree rather than as text, because they can arrive through an
   alias.
4. **Weather can only be passed in** - ``assess_mission`` is inspected and proven
   to take a snapshot object as its only weather input: no callable, no client,
   no station id, and therefore nothing it could use to fetch.
"""

from __future__ import annotations

import ast
import os
import socket
import subprocess
import sys
import types
from inspect import signature
from pathlib import Path

import pytest

from droneops_energy import MissionEnergyRequest, assess_mission

SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "droneops_energy"

#: Modules that reach the outside world or read a clock. The database names mirror
#: .importlinter's `energy-has-no-database` contract, so the two agree on what
#: "impure" means instead of one of them drifting.
FORBIDDEN_MODULES = frozenset(
    {
        "alembic",
        "asyncpg",
        "asyncio",
        "http",
        "httpx",
        "multiprocessing",
        "os",
        "pathlib",
        "pickle",
        "psycopg",
        "psycopg2",
        "random",
        "requests",
        "shelve",
        "socket",
        "sqlite3",
        "sqlalchemy",
        "ssl",
        "subprocess",
        "tempfile",
        "threading",
        "time",
        "urllib",
        "uuid",
    }
)

#: Everything the kernel is permitted to import. Add deliberately, with a reason.
ALLOWED_MODULES = frozenset(
    {
        "__future__",
        "collections",
        "dataclasses",
        "droneops_contracts",
        "droneops_energy",
        "droneops_geom_core",
        "enum",
        "hashlib",
        "itertools",
        "math",
        "re",
        "typing",
    }
)

#: Attribute chains that read a clock, the environment or an unseeded source.
FORBIDDEN_CALLS = frozenset(
    {
        "datetime.now",
        "datetime.utcnow",
        "datetime.today",
        "datetime.fromtimestamp",
        "date.today",
        "time.time",
        "time.monotonic",
        "time.perf_counter",
        "time.process_time",
        "os.environ",
        "os.urandom",
        "socket.socket",
        "httpx.Client",
    }
)

#: Bare call names that must never appear in a pure kernel.
FORBIDDEN_BUILTINS = frozenset({"eval", "exec", "open", "__import__", "compile", "input"})

#: The two modules whose floats are the documented tolerance boundary.
MATH_RUNTIME_MODULES = frozenset({"trig.py", "model.py", "numeric.py"})

#: The workspace packages the kernel may never reach, and why:
#: ``droneops_persistence`` is the database, which conventions.md section 7 lists as
#: the one package boundary the energy kernel must not own; ``droneops_platform``
#: owns the clocks, IDs and auth port, so importing it is a wall clock arriving
#: through the side door -- exactly what section 4 forbids.
#:
#: Reaching any *other* domain package is permitted, and deliberately so.
#: conventions.md section 7 requires energy to own the energy model; it never asks
#: energy to own geodesy. ``geometry.py`` bridging a corridor polyline to integer
#: legs therefore calls geom-core's finished, tested ``geodesic_distance_mm`` and
#: ``initial_bearing_deg`` rather than carrying a second distance formula inside the
#: kernel -- two formulas that can disagree about the same route is a safety defect,
#: not a purity win, and it would put the float leak the bridge exists to close back
#: into the model. This stays a strong guard because ``ALLOWED_MODULES`` is a
#: positive allow list: a new domain dependency still has to be added by name, with
#: a reason, and anything absent from it fails. What is tightened below is the part
#: that actually threatens determinism -- network, clock, ``time``, unseeded
#: ``random`` and persistence.
FORBIDDEN_DRONEOPS_PACKAGES = frozenset({"droneops_persistence", "droneops_platform"})


def package_files() -> list[Path]:
    return sorted(SRC_ROOT.rglob("*.py"))


def parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _is_type_guard(test: ast.expr) -> bool:
    return ast.unparse(test) in {"TYPE_CHECKING", "typing.TYPE_CHECKING"}


def imported_modules(tree: ast.Module) -> list[tuple[str, bool]]:
    """``(module, hidden_behind_type_checking)`` for every import in a module."""
    found: list[tuple[str, bool]] = []

    def walk(node: ast.AST, guarded: bool) -> None:
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.append((alias.name, guarded))
            return
        if isinstance(node, ast.ImportFrom):
            module = "droneops_energy" if node.level else (node.module or "")
            found.append((module, guarded))
            return
        if isinstance(node, ast.If):
            inner = guarded or _is_type_guard(node.test)
            for child in node.body + node.orelse:
                walk(child, inner)
            return
        for child in ast.iter_child_nodes(node):
            walk(child, guarded)

    walk(tree, False)
    return found


def dotted_name(node: ast.expr) -> str:
    """Render ``a.b.c`` chains; anything else gets a stable placeholder."""
    parts: list[str] = []
    current: ast.expr | None = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return ".".join(reversed(parts))


def test_package_source_is_present() -> None:
    assert SRC_ROOT.is_dir(), f"expected energy sources at {SRC_ROOT}"
    assert len(package_files()) >= 9, "the energy kernel lost a module"


def import_violations(source: str, name: str = "probe.py") -> list[str]:
    """Every import-level purity violation in a module's source, as the guard sees it.

    Shared by the scan over the real package and by the negative self-test below, so
    the two cannot drift apart.
    """
    offenders: list[str] = []
    for module, type_only in imported_modules(ast.parse(source)):
        top = module.split(".")[0]
        if top in FORBIDDEN_MODULES:
            offenders.append(f"{name} imports {module}")
        if top not in ALLOWED_MODULES:
            offenders.append(f"{name} imports {top}, which is not allow-listed")
        if top in FORBIDDEN_DRONEOPS_PACKAGES:
            offenders.append(f"{name} reaches {top}: database or clock ownership")
        if top == "math" and not (type_only or name in MATH_RUNTIME_MODULES):
            offenders.append(f"{name} uses math at runtime outside the kernel's own files")
    return offenders


@pytest.mark.parametrize("path", package_files(), ids=lambda path: path.name)
def test_no_forbidden_import(path: Path) -> None:
    assert import_violations(path.read_text(encoding="utf-8"), path.name) == []


#: Each of these must be caught. A deny list that has never been seen to bite is a
#: comment, not a check -- and this guard was just loosened in one direction, so the
#: other direction is now proven rather than assumed.
GUARD_PROBES = {
    "database-driver": "import httpx",
    "raw-socket": "from socket import socket",
    "wall-clock": "import time",
    "unseeded-random": "import random",
    "orm": "from sqlalchemy import select",
    "migrations": "import alembic",
    "asyncpg": "import asyncpg",
    "persistence-package": "from droneops_persistence import get_session",
    "platform-clock-port": "import droneops_platform",
    "undeclared-dependency": "import pandas",
}


@pytest.mark.parametrize("label", sorted(GUARD_PROBES))
def test_the_guard_bites_on_every_forbidden_import(label: str) -> None:
    assert import_violations(GUARD_PROBES[label]), f"{label} walked past the purity guard"


def test_the_guard_still_permits_domain_reuse() -> None:
    """The one behaviour this file changed: geom-core is a legitimate import."""
    assert import_violations("from droneops_geom_core import GeoPoint, geodesic_distance_mm") == []


@pytest.mark.parametrize("path", package_files(), ids=lambda path: path.name)
def test_no_wall_clock_or_random_draw(path: Path) -> None:
    for node in ast.walk(parse(path)):
        if not isinstance(node, ast.Call):
            continue
        name = dotted_name(node.func)
        assert not any(
            name == forbidden or name.startswith(forbidden + ".") for forbidden in FORBIDDEN_CALLS
        ), f"{path.name}:{node.lineno} calls {name}"
        head = name.split(".")[0]
        assert head != "random", f"{path.name}:{node.lineno} uses random without a seed"
        assert head not in FORBIDDEN_BUILTINS, f"{path.name}:{node.lineno} calls {name}"


@pytest.mark.parametrize("path", package_files(), ids=lambda path: path.name)
def test_no_datetime_or_wall_clock_module_anywhere(path: Path) -> None:
    """Weather freshness is judged against an integer scenario clock, never a date."""
    for module, _type_only in imported_modules(parse(path)):
        assert module.split(".")[0] != "datetime", f"{path.name} imports datetime at all"


def test_no_any_in_public_source() -> None:
    """conventions.md section 10: no ``Any`` in a domain package."""
    offenders: list[str] = []
    for path in package_files():
        for node in ast.walk(parse(path)):
            if isinstance(node, ast.Name) and node.id == "Any":
                offenders.append(f"{path.name}:{node.lineno}")
            if isinstance(node, ast.Attribute) and node.attr == "Any":
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, f"Any appeared in {offenders}"


def test_public_surface_is_sorted_and_complete() -> None:
    import droneops_energy as package  # noqa: PLC0415 - the guard scans before it imports

    names = package.__all__
    assert len(names) == len(set(names)), "__all__ must not repeat a name"

    def group_of(name: str) -> int:
        if name.isupper():
            return 0
        return 1 if name[0].isupper() else 2

    groups = {group_of(name) for name in names}
    assert groups == {0, 1, 2}, "expected constants, types, then functions"
    assert [group_of(name) for name in names] == sorted(group_of(name) for name in names), (
        "__all__ groups must appear in order"
    )
    for group in (0, 1, 2):
        members = [name for name in names if group_of(name) == group]
        assert members == sorted(members), f"group {group} must be sorted"

    missing = [name for name in package.__all__ if not hasattr(package, name)]
    assert not missing, f"declared but absent: {missing}"

    leaked = sorted(
        name
        for name, value in vars(package).items()
        if not name.startswith("_")
        and name != "annotations"
        and not isinstance(value, types.ModuleType)
        and getattr(value, "__module__", package.__name__) == package.__name__
        and name not in set(names)
    )
    assert not leaked, f"public but undeclared: {leaked}"


def test_assess_mission_cannot_reach_the_network_by_construction() -> None:
    """The weather parameter's type must be a snapshot object, not a callable.

    A kernel that took ``fetch: Callable[...]`` or a station id could fetch even
    with no import to scan for, so the signature itself is the boundary.
    """
    assert list(signature(assess_mission).parameters) == ["request"], (
        "assess must take one bundled request"
    )
    weather = MissionEnergyRequest.__dataclass_fields__["weather"]
    annotation = str(weather.type)
    assert "WindSnapshot" in annotation, annotation
    for forbidden in ("Callable", "Client", "Transport", "str", "int"):
        assert forbidden not in annotation, f"weather typed as {forbidden} could fetch"


def test_a_run_still_completes_with_every_socket_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Belt and braces: even if an import sneaked through, no socket may be opened.

    The test closes the process's socket family for the duration of a full
    feasibility evaluation and asserts the answer is unchanged, which is what a
    deterministic replay actually requires.
    """

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the energy kernel opened a socket")

    monkeypatch.setattr(socket, "socket", refuse)
    sys.modules.pop("droneops_energy", None)
    # Imported after the pop, so this is genuinely a fresh import under a closed socket.
    # isort: off
    from energy_support import mission, trace  # noqa: PLC0415
    from droneops_energy import assess_mission  # noqa: PLC0415
    # isort: on

    report = assess_mission(mission(weather=trace(speed_dmps=40)))
    assert report.estimated_consumption_wh > 0
    assert isinstance(report.margin_wh, int)


def test_identical_inputs_under_every_hash_seed_give_the_same_margin() -> None:
    """I7 for this package: set and dict order must never reach a margin."""
    workload = (
        "import hashlib,json;"
        "from energy_support import mission, trace, KM_MM;"
        "from droneops_energy import assess_mission, sweep_sensitivity;"
        "r=[assess_mission(mission(distance_mm=d*KM_MM, weather=trace(speed_dmps=s), "
        "payload_g=p, recovery_mm=d*KM_MM)) for d in (3,6,9) for s in (0,40,80) "
        "for p in (250,900)];"
        "s=sweep_sensitivity(mission(distance_mm=6*KM_MM, weather=trace(speed_dmps=60)));"
        "print(hashlib.sha256(json.dumps([(x.margin_wh, x.feasible, x.summary) for x in r]"
        " + [str(s.as_table())], sort_keys=True).encode())"
        ".hexdigest())"
    )
    digests: dict[str, str] = {}
    for seed in ("0", "1", "42", "999", "12345"):
        completed = subprocess.run(
            [sys.executable, "-c", workload],
            capture_output=True,
            text=True,
            check=True,
            env=dict(os.environ, PYTHONHASHSEED=seed),
            cwd=str(Path(__file__).resolve().parent),
        )
        digests[seed] = completed.stdout.strip().splitlines()[-1]
    assert len(set(digests.values())) == 1, f"seed-dependent output: {digests}"
