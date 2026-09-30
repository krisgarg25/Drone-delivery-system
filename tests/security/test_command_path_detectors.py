"""The no-command detectors must reject a synthetic command path.

A guard that never fires is worse than no guard, because it invites the team to believe the
boundary exists. These tests build a deliberately illegal tree and contract, feed them to
the same code CI runs, and assert the detectors refuse them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import check_boundaries, openapi_gate

REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.security


def test_command_route_is_detected_on_both_sides() -> None:
    spec = {
        "paths": {
            "/v1/missions/{id}/launch": {"post": {"operationId": "launchMission"}},
            "/v1/missions/{id}/return-to-launch": {"post": {"operationId": "rtlMission"}},
            "/v1/missions/{id}/parameters": {"put": {"operationId": "setParameter"}},
        }
    }
    hits = openapi_gate.command_violations(openapi_gate.operation_tokens(spec), "synthetic")
    assert len(hits) >= 3
    assert any("launch" in hit for hit in hits)
    assert any("parameter" in hit for hit in hits)


def test_legitimate_simulation_routes_are_not_flagged() -> None:
    spec = {
        "paths": {
            "/v1/orders": {"post": {"operationId": "registerOrder"}},
            "/v1/missions/{id}/plan-versions": {"get": {"operationId": "listPlanVersions"}},
            "/health": {"get": {"operationId": "health"}},
        }
    }
    assert openapi_gate.command_violations(openapi_gate.operation_tokens(spec), "real") == []


def test_command_module_in_the_source_tree_is_detected(tmp_path: Path) -> None:
    for relative in (
        "apps/api/src/droneops_api/commands.py",
        "adapters/vehicle-mavlink/src/droneops_adapters_vehicle_mavlink/offboard.py",
        "packages/planner/src/droneops_planner/mission_upload.py",
    ):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("def forbidden(): ...\n", encoding="utf-8")

    hits = check_boundaries.find_command_artifacts(tmp_path)
    assert len(hits) == 3, hits


def test_a_file_naming_the_prohibition_is_not_a_violation(tmp_path: Path) -> None:
    for relative in (
        "apps/api/src/droneops_api/no_command_guard.py",
        "packages/contracts/src/droneops_contracts/forbidden_operations.py",
    ):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("ALLOWED = True\n", encoding="utf-8")

    assert check_boundaries.find_command_artifacts(tmp_path) == []


def test_boundary_contracts_are_declared_in_the_real_repo() -> None:
    assert check_boundaries.check_contracts_declared(REPO_ROOT / ".importlinter") == []


def _drop_line_from_contract(text: str, contract: str, module: str) -> str:
    """Delete one forbidden-module line, scoped to a single contract section."""
    header = f"[importlinter:contract:{contract}]"
    start = text.index(header)
    end = text.find("\n[", start + len(header))
    section = text[start:] if end == -1 else text[start:end]
    stripped = section.replace(f"    {module}\n", "", 1)
    assert stripped != section, f"{module} is not listed in {contract}"
    return text[:start] + stripped + (text[end:] if end != -1 else "")


def test_deleting_one_contract_line_is_caught(tmp_path: Path) -> None:
    copied = tmp_path / ".importlinter"
    text = (REPO_ROOT / ".importlinter").read_text(encoding="utf-8")
    copied.write_text(
        _drop_line_from_contract(text, "conflict-checker-independent", "droneops_geom_4d"),
        encoding="utf-8",
    )

    problems = check_boundaries.check_contracts_declared(copied)
    assert any("droneops_geom_4d" in problem for problem in problems), problems


def test_renaming_a_contract_is_caught(tmp_path: Path) -> None:
    copied = tmp_path / ".importlinter"
    text = (REPO_ROOT / ".importlinter").read_text(encoding="utf-8")
    copied.write_text(
        text.replace("[importlinter:contract:sim-core-has-no-network-or-database]", "[importlinter:contract:sim-lite]", 1),
        encoding="utf-8",
    )

    problems = check_boundaries.check_contracts_declared(copied)
    assert any("sim-core-has-no-network-or-database" in problem for problem in problems), problems
