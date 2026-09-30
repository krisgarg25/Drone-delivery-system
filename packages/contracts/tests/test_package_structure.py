"""Unit tests for the contracts package structure and purity."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import droneops_contracts as c

SRC = Path(c.__file__).resolve().parent

REQUIRED_MODULES = [
    "mission",
    "order",
    "reservation",
    "route",
    "energy",
    "telemetry",
    "decision",
    "usage",
    "events",
    "errors",
    "provenance",
    "geometry",
    "policy",
]

FORBIDDEN_ROOTS = {
    "fastapi",
    "sqlalchemy",
    "httpx",
    "flask",
    "droneops_persistence",
    "droneops_adapters_vehicle_mavlink",
    "requests",
    "psycopg2",
    "alembic",
}

LEGACY_NAMES = [
    "MissionStatus",
    "ReservationState",
    "AltitudeReference",
    "PriorityClass",
    "TimeWindow",
    "SimulatedOrderRequest",
    "SimulatedOrderResponse",
]


@pytest.mark.parametrize("module", REQUIRED_MODULES)
def test_module_exists(module: str) -> None:
    assert (SRC / f"{module}.py").is_file()


def test_all_required_modules_present() -> None:
    assert set(REQUIRED_MODULES) <= {p.stem for p in SRC.glob("*.py") if p.stem != "__init__"}


def _imported_roots(tree: ast.AST) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".", 1)[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                roots.add(node.module.split(".", 1)[0])
    return roots


@pytest.mark.parametrize("module", REQUIRED_MODULES + ["fleet", "facilities", "scenario"])
def test_no_framework_or_db_imports(module: str) -> None:
    tree = ast.parse((SRC / f"{module}.py").read_text(encoding="utf-8"))
    roots = _imported_roots(tree)
    assert not (roots & FORBIDDEN_ROOTS), f"{module} imports {roots & FORBIDDEN_ROOTS}"


@pytest.mark.parametrize("name", LEGACY_NAMES)
def test_legacy_names_remain_public(name: str) -> None:
    assert hasattr(c, name)
    assert name in c.__all__


def test_core_entities_are_modelled() -> None:
    for entity in [
        "AircraftModel", "Aircraft", "AircraftState", "MaintenanceEvent", "Facility",
        "Pad", "ChargerBay", "CrewAssignment", "Order", "Mission", "PlanVersion",
        "DecisionRecord", "Reservation", "ResourceCatalog", "TelemetryObservation",
        "EventEnvelope", "Outbox", "AuditLogEntry",
    ]:
        assert hasattr(c, entity), f"{entity} not exported"
