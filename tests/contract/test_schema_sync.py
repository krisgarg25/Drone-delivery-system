"""One-source check: committed JSON Schemas equal the generated model schemas.

The Python models are the single source of truth. Each file under
``contracts/json-schema`` must match ``Model.model_json_schema()`` exactly, so a
change to a model cannot silently drift from the published schema.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from droneops_contracts import (
    DecisionRecord,
    Mission,
    PlanVersion,
    Reservation,
    ScenarioBundle,
    SimulatedOrderRequest,
    TelemetryObservation,
)

SCHEMA_DIR = Path(__file__).resolve().parents[2] / "contracts" / "json-schema"

MODEL_BY_FILE: dict[str, type] = {
    "mission.json": Mission,
    "reservation.json": Reservation,
    "telemetry_observation.json": TelemetryObservation,
    "decision_record.json": DecisionRecord,
    "plan_version.json": PlanVersion,
    "order_request.json": SimulatedOrderRequest,
    "scenario_bundle.json": ScenarioBundle,
}


@pytest.mark.parametrize("filename", sorted(MODEL_BY_FILE))
def test_schema_matches_model(filename: str) -> None:
    model = MODEL_BY_FILE[filename]
    on_disk = json.loads((SCHEMA_DIR / filename).read_text(encoding="utf-8"))
    assert on_disk == model.model_json_schema(), f"{filename} drifted from {model.__name__}"


def test_all_expected_schema_files_present() -> None:
    present = {p.name for p in SCHEMA_DIR.glob("*.json")}
    assert set(MODEL_BY_FILE) <= present


def test_model_schema_names_unique() -> None:
    assert len(MODEL_BY_FILE) == len(set(MODEL_BY_FILE.values()))
