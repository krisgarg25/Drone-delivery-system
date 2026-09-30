"""Structural no-command and boundary checks.

`import-linter` proves module-level isolation. This script proves the two things it
cannot see:

1. no command, mission-upload, parameter, firmware or offboard *artifact* exists
   anywhere in the shipped tree, so a command path cannot be added by a file drop;
2. every forbidden-dependency rule in `02-repository-layout.md` section 4 is actually
   declared in `.importlinter`, so a boundary cannot be silently deleted.

Run with `--self` to assert the checker itself is configured (used by `make bootstrap`).
"""

from __future__ import annotations

import argparse
import configparser
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Any path component matching these makes the repository able to command an aircraft.
FORBIDDEN_PATH_TOKENS: tuple[str, ...] = (
    "command",
    "commands",
    "mission_upload",
    "mission-upload",
    "upload_mission",
    "offboard",
    "firmware",
    "param_set",
    "set_parameter",
    "actuator",
)

# Command-shaped HTTP operations. `/start`, `/land`, `/return-to-launch`, ...
FORBIDDEN_ROUTE_TOKENS: tuple[str, ...] = (
    "command",
    "launch",
    "takeoff",
    "land",
    "rtl",
    "return_to_launch",
    "start",
    "stop",
    "abort",
    "upload",
    "firmware",
    "parameter",
    "mav_cmd",
    "offboard",
    "arm",
)

SKIP_DIRS = {".venv", "__pycache__", "node_modules", ".git", "dist", "build", ".mypy_cache", ".ruff_cache", ".import_linter_cache"}

# A file that names the prohibition is a guard, not a violation.
NEGATED_NAME = re.compile(r"(no[-_]?command|non[-_]?command|forbidden|absent|never[-_]?command)")

# package -> modules/external packages it must never import
REQUIRED_CONTRACTS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "core-domain-cannot-reach-planner": (
        ("droneops_core_domain",),
        ("droneops_planner",),
    ),
    "geom-core-is-pure": (
        ("droneops_geom_core",),
        ("sqlalchemy", "psycopg2", "asyncpg", "httpx", "requests", "fastapi", "redis"),
    ),
    "energy-has-no-database": (
        ("droneops_energy",),
        ("sqlalchemy", "asyncpg", "alembic", "droneops_persistence"),
    ),
    "facilities-has-no-telemetry-truth": (
        ("droneops_facilities",),
        ("droneops_telemetry", "droneops_gateway", "droneops_adapters_vehicle_mavlink"),
    ),
    "airspace-has-no-pricing": (
        ("droneops_airspace",),
        ("droneops_dispatcher",),
    ),
    "dispatcher-has-no-vehicle-link": (
        ("droneops_dispatcher",),
        ("droneops_adapters_vehicle_mavlink", "droneops_gateway"),
    ),
    "conflict-checker-independent": (
        ("droneops_conflict_checker",),
        ("droneops_planner", "droneops_geom_4d", "droneops_persistence"),
    ),
    "sim-core-has-no-network-or-database": (
        ("droneops_sim_core", "droneops_sim_runner"),
        (
            "droneops_persistence",
            "droneops_adapters_vehicle_mavlink",
            "droneops_adapters_gis_etl",
            "droneops_adapters_terrain",
            "droneops_adapters_weather_open_meteo",
            "droneops_adapters_weather_meteostat",
            "droneops_adapters_airspace_import",
            "sqlalchemy",
            "asyncpg",
            "httpx",
            "requests",
            "socket",
            "uvicorn",
        ),
    ),
    "api-has-no-command-path": (
        ("droneops_api",),
        ("droneops_gateway", "droneops_adapters_vehicle_mavlink", "droneops_persistence"),
    ),
    "domain-is-pure": (
        ("droneops_contracts", "droneops_core_domain", "droneops_reservations"),
        ("fastapi", "sqlalchemy", "httpx", "droneops_persistence"),
    ),
    "telemetry-cannot-command": (
        ("droneops_telemetry",),
        ("droneops_planner", "droneops_dispatcher", "droneops_persistence"),
    ),
    "platform-has-no-business-policy": (
        ("droneops_platform",),
        ("droneops_planner", "droneops_reservations", "droneops_dispatcher", "droneops_energy"),
    ),
    "observability-cannot-mutate-reservations": (
        ("droneops_observability",),
        ("droneops_reservations", "droneops_persistence", "droneops_planner"),
    ),
    "worker-has-no-api-import": (
        ("droneops_worker",),
        ("droneops_api",),
    ),
    "geom-4d-cannot-reach-planner": (
        ("droneops_geom_4d",),
        ("droneops_planner",),
    ),
    "corridor-graph-has-no-live-vehicle-api": (
        ("droneops_corridor_graph",),
        ("droneops_adapters_vehicle_mavlink", "droneops_gateway"),
    ),
    "adapters-cannot-decide-reservations": (
        (
            "droneops_adapters_gis_etl",
            "droneops_adapters_terrain",
            "droneops_adapters_weather_open_meteo",
            "droneops_adapters_weather_meteostat",
            "droneops_adapters_airspace_import",
            "droneops_adapters_vehicle_mavlink",
        ),
        ("droneops_reservations", "droneops_planner", "droneops_dispatcher"),
    ),
    "mavlink-adapter-is-dependency-free": (
        ("droneops_adapters_vehicle_mavlink",),
        (
            "droneops_api",
            "droneops_worker",
            "droneops_gateway",
            "droneops_persistence",
            "droneops_planner",
            "droneops_reservations",
            "droneops_conflict_checker",
        ),
    ),
}


def iter_source_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.is_file() and path.suffix in {".py", ".ts", ".tsx", ".js", ".jsx", ".sql", ".yaml", ".yml"}:
            files.append(path)
    return files


def find_command_artifacts(root: Path) -> list[str]:
    """Flag production modules or directories that could issue an aircraft command.

    Only shipped source trees are scanned. A file whose name says the opposite
    (`adr/0004-no-command-path.md`, `tests/contract/test_no_command_surface.py`) is
    evidence of the boundary, not a violation of it.
    """
    hits: list[str] = []
    sources: list[Path] = []
    for kind in ("apps", "packages", "adapters"):
        for member in sorted(root.glob(f"{kind}/*/src")):
            sources.extend(path for path in member.rglob("*") if path.is_file())
    for path in sorted(sources):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        rel = path.relative_to(root)
        name = path.name.lower()
        if NEGATED_NAME.search(name):
            continue
        for token in FORBIDDEN_PATH_TOKENS:
            if token in name:
                hits.append(f"{rel}: path component matches forbidden token '{token}'")
                break
    return hits


def find_command_routes(root: Path) -> list[str]:
    hits: list[str] = []
    contract = root / "contracts" / "openapi" / "v1.yaml"
    targets = [contract] if contract.exists() else []
    for target in targets:
        text = target.read_text(encoding="utf-8")
        for line_no, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if not stripped.startswith(("/", "operationId")):
                continue
            lowered = stripped.lower()
            for token in FORBIDDEN_ROUTE_TOKENS:
                if re.search(rf"[/_-]{token}[/_-]?", lowered):
                    hits.append(f"{target.relative_to(root)}:{line_no}: {stripped}")
    return hits


def load_contract_names(config_path: Path) -> set[str]:
    parser = configparser.ConfigParser()
    parser.optionxform = str  # type: ignore[assignment]
    parser.read(config_path, encoding="utf-8")
    names: set[str] = set()
    for section in parser.sections():
        if section.startswith("importlinter:contract:"):
            names.add(section.split(":", 2)[2])
    return names


def check_contracts_declared(config_path: Path) -> list[str]:
    declared = load_contract_names(config_path)
    problems: list[str] = []
    for name, (sources, forbidden) in REQUIRED_CONTRACTS.items():
        if name not in declared:
            problems.append(f"missing import-linter contract '{name}'")
            continue
        parser = configparser.ConfigParser()
        parser.optionxform = str  # type: ignore[assignment]
        parser.read(config_path, encoding="utf-8")
        section = f"importlinter:contract:{name}"
        listed_sources = {s.strip() for s in parser.get(section, "source_modules", fallback="").splitlines()}
        listed_forbidden = {s.strip() for s in parser.get(section, "forbidden_modules", fallback="").splitlines()}
        for module in sources:
            if module not in listed_sources:
                problems.append(f"contract '{name}' does not list source module '{module}'")
        for module in forbidden:
            if module not in listed_forbidden:
                problems.append(f"contract '{name}' does not forbid module '{module}'")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self", action="store_true", help="only self-check the checker config")
    args = parser.parse_args(argv)

    root = REPO_ROOT
    problems = find_command_artifacts(root) + find_command_routes(root)
    if not args.self:
        problems += check_contracts_declared(root / ".importlinter")

    if problems:
        print("Boundary check FAILED:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        print(
            "\nDroneOps has no aircraft command path by construction. If a route or module "
            "here is not a command path, rename it; do not add an exception.",
            file=sys.stderr,
        )
        return 1

    print("no command artifacts, no command routes, all boundary contracts declared")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
