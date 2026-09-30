"""Determinism and purity guard for the geom-core package.

conventions.md section 4 makes non-determinism a defect that fails the CI replay
gate, and ``technical-build/01`` section 21 requires the kernel to have no
network, filesystem or wall-clock access. Those are structural properties, so they
are checked structurally rather than trusted: this file reads the package source
and fails if a forbidden construct appears anywhere in it.

Four independent guards:

1. **Import deny list** - the side-effecting modules named in the task: ``socket``,
   ``urllib``, ``requests``, ``httpx``, ``sqlalchemy``, ``asyncio``, ``random``,
   plus ``time``, ``os``, ``sys``, ``pathlib``, ``subprocess``, ``threading`` and
   the rest of the reachable outside world.
2. **Import allow list** - the inverse check, so a dependency cannot be introduced
   by simply not thinking of it in advance.
3. **Call scan** - wall-clock reads and unseeded randomness detected on the
   *syntax tree*, not as text, because they can arrive through an alias.
4. **Hash-seed replay** - one workload executed in a subprocess under several
   ``PYTHONHASHSEED`` values. Set and dict iteration order changes with the seed,
   so output that depends on it differs here and fails (invariant I7).
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "droneops_geom_core"

#: Modules that reach the outside world or read a clock.
FORBIDDEN_MODULES = frozenset(
    {
        "asyncio",
        "http",
        "httpx",
        "multiprocessing",
        "os",
        "pathlib",
        "pickle",
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
        "bisect",
        "collections",
        "dataclasses",
        "datetime",
        "decimal",
        "droneops_geom_core",
        "enum",
        "math",
        "re",
        "typing",
    }
)

#: The only module allowed to hold ``datetime`` at runtime: the conversion boundary.
DATETIME_RUNTIME_MODULES = frozenset({"timebase.py"})

#: Attribute chains that read a clock, an environment or an unseeded source.
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
        "os.getrandom",
    }
)

#: Bare call names that must never appear in a pure kernel.
FORBIDDEN_BUILTINS = frozenset({"eval", "exec", "open", "__import__", "compile", "input"})


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
            module = "droneops_geom_core" if node.level else (node.module or "")
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
    """Render ``a.b.c`` attribute chains; anything else gets a stable placeholder."""
    parts: list[str] = []
    current: ast.expr | None = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return ".".join(reversed(parts))


def test_package_source_is_present() -> None:
    assert SRC_ROOT.is_dir(), f"expected geom-core sources at {SRC_ROOT}"
    assert len(package_files()) >= 9, "geom-core lost a module"


@pytest.mark.parametrize("path", package_files(), ids=lambda path: path.name)
def test_no_forbidden_import(path: Path) -> None:
    for module, type_only in imported_modules(parse(path)):
        top = module.split(".")[0]
        assert top not in FORBIDDEN_MODULES, f"{path.name} imports {module}"
        assert top in ALLOWED_MODULES, f"{path.name} imports {top}, which is not allow-listed"
        if top == "datetime":
            allowed_here = path.name in DATETIME_RUNTIME_MODULES
            assert type_only or allowed_here, (
                f"{path.name} uses datetime at runtime; only timebase.py may"
            )


@pytest.mark.parametrize("path", package_files(), ids=lambda path: path.name)
def test_no_wall_clock_or_random_draw(path: Path) -> None:
    tree = parse(path)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = dotted_name(node.func)
        assert not any(
            name == forbidden or name.startswith(forbidden + ".") for forbidden in FORBIDDEN_CALLS
        ), f"{path.name}:{node.lineno} calls {name}"
        head = name.split(".")[0]
        assert head != "random", f"{path.name}:{node.lineno} uses random without a seed"
        assert head not in FORBIDDEN_BUILTINS, f"{path.name}:{node.lineno} calls {name}"


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


def test_datetime_crosses_only_at_the_documented_boundary() -> None:
    """Exactly one runtime module touches ``datetime``, and it is the boundary."""
    runtime = {
        path.name
        for path in package_files()
        for module, type_only in imported_modules(parse(path))
        if module.split(".")[0] == "datetime" and not type_only
    }
    assert runtime == set(DATETIME_RUNTIME_MODULES)


def test_no_module_import_is_unconditional_relative_import_of_another_domain() -> None:
    offenders: list[str] = []
    for path in package_files():
        for node in ast.walk(parse(path)):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module.startswith(("droneops_contracts", "droneops_planner")):
                    offenders.append(f"{path.name}:{node.module}")
    assert not offenders, f"geom-core must stay dependency-free, found {offenders}"


def test_public_surface_is_sorted_and_complete() -> None:
    import droneops_geom_core as package

    names = package.__all__
    assert len(names) == len(set(names)), "__all__ must not repeat a name"

    # `__all__` is grouped constants, then types, then functions. Strict
    # `sorted()` would interleave them by code point, so require ordering inside
    # each group instead.
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

    declared = set(names)
    namespace = vars(package)
    leaked = sorted(
        name
        for name, value in namespace.items()
        if not name.startswith("_")
        and name != "annotations"
        and not isinstance(value, types.ModuleType)
        and getattr(value, "__module__", package.__name__) == package.__name__
        and name not in declared
    )
    assert not leaked, f"public but undeclared: {leaked}"

    # The Phase 0 stub keeps its name and its boolean overlap method.
    assert hasattr(package.Interval, "overlaps")
    assert hasattr(package, "compute_safe_intervals")


WORKLOAD = """
import hashlib
from droneops_geom_core import *

digest = hashlib.sha256()

def feed(key, value):
    digest.update(key.encode())
    digest.update(repr(value).encode())

intervals = [Interval.of(i * 37 % 5000, i * 37 % 5000 + 200 + i % 7) for i in range(400)]
feed("merge", merge(intervals))
feed("merge-reversed", merge(list(reversed(intervals))))
feed("total", total_measure(intervals))
feed("gaps", gaps_within(Interval.of(0, 6000), intervals))

kinds = list(ResourceKind)
keys = []
for index in range(240):
    kind = kinds[index % 6]
    window = Interval.of(index * 13, index * 13 + 100)
    identifier = "id-%d-%s" % (index % 23, "x" * (index % 5))
    keys.append(resource_key(kind, identifier, window=window if kind.takes_window else None))
feed("lock-order", lock_order(keys))
feed("lock-order-shuffled", lock_order(list(reversed(keys))))

spec = GridSpec(32643, Millimetre(16000), Millimetre(10000))
cells = [spec.volume_cell_of(i * 917 - 90000, i * -613 + 40000, i * 71 - 5000,
                             level=(i % 6) + 1, reference=AltitudeReference.AGL) for i in range(60)]
feed("cells", merge_cells_by_token(cells))
# Level 6 keeps the indices fine-grained and well inside the declared grid domain.
feed("neighbours", [c.token() for c in cells[5].neighbourhood(radius=2)])
feed("ring", [c.token() for c in cells[5].ring(radius=2)])

bands = [AltitudeBand(AltitudeReference.AGL, Millimetre(i * 900 % 90000),
                      Millimetre(i * 900 % 90000 + 9000)) for i in range(120)]
feed("bands", merge_bands(bands))
feed("bands-shuffled", merge_bands(list(reversed(bands))))

query = SafeIntervalQuery.build(
    resource_key('volume_cell', 'kpbpcc.08', window=Interval.of(0, 1000000)),
    horizon=Interval.of(0, 50000), traversal_us=1200,
    padding=SafeIntervalPadding(Microsecond(300), Microsecond(700)),
    occupied=[(Interval.of(i * 4000, i * 4000 + 900), 'r%d' % i) for i in range(0, 12)]
    + [(Interval.of(i * 4000 + 300, i * 4000 + 1500), 'q%d' % i) for i in range(20, 26)],
)
result = compute_safe_intervals(query)
feed("windows", [(w.entry.start, w.entry.end) for w in result.windows])
feed("blocked", result.blocked)
feed("audit", [(a.reason, a.available_us) for a in result.gap_audit])
feed("merged", [o.label for o in query.merged_occupied()])

points = [GeoPoint(30.7 + i / 1000.0, 76.79 + i / 2000.0) for i in range(50)]
feed("distance", [geodesic_distance_mm(points[0], point) for point in points])
feed("cells-from-geodetic",
     [LocalPlane(points[0]).cell_of(spec, point).key() for point in points])

print(digest.hexdigest())
"""


def test_output_is_identical_under_every_hash_seed() -> None:
    """I7: set and dict iteration order must never be able to reach the output."""
    digests: dict[str, str] = {}
    for seed in ("0", "1", "42", "999", "12345"):
        environment = dict(os.environ, PYTHONHASHSEED=seed)
        completed = subprocess.run(
            [sys.executable, "-c", WORKLOAD],
            capture_output=True,
            text=True,
            check=True,
            env=environment,
            cwd=str(SRC_ROOT.parents[1]),
        )
        digests[seed] = completed.stdout.strip().splitlines()[-1]
    assert len(set(digests.values())) == 1, f"seed-dependent output: {digests}"


def test_workload_actually_exercises_the_package() -> None:
    """Guards the guard: an empty subprocess would pass the comparison trivially."""
    environment = dict(os.environ, PYTHONHASHSEED="7")
    completed = subprocess.run(
        [sys.executable, "-c", WORKLOAD],
        capture_output=True,
        text=True,
        check=True,
        env=environment,
        cwd=str(SRC_ROOT.parents[1]),
    )
    digest = completed.stdout.strip().splitlines()[-1]
    assert len(digest) == 64
    assert int(digest, 16) != 0
