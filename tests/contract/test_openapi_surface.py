"""Public API surface coverage for Phase 1 and the Phase 4 console."""

from __future__ import annotations

from pathlib import Path

import yaml

OPENAPI_PATH = Path(__file__).resolve().parents[2] / "contracts" / "openapi" / "v1.yaml"
DOC = yaml.safe_load(OPENAPI_PATH.read_text(encoding="utf-8"))
PATHS = DOC["paths"]

REQUIRED_OPERATIONS = {
    # orders
    ("/v1/orders", "post"),
    ("/v1/orders/{id}", "get"),
    ("/v1/orders/{id}/cancel", "post"),
    # missions and planning
    ("/v1/missions/{id}/plan", "post"),
    ("/v1/missions/{id}/plan-versions", "get"),
    ("/v1/missions/{id}/reserve", "post"),
    ("/v1/missions/{id}/decision", "get"),
    ("/v1/missions/{id}/cancel", "post"),
    # fleet and facilities
    ("/v1/aircraft", "get"),
    ("/v1/aircraft/{id}", "get"),
    ("/v1/facilities", "get"),
    ("/v1/facilities/{id}/capacity", "get"),
    ("/v1/facilities/{id}/outage", "post"),
    # reservations and monitoring
    ("/v1/reservations", "get"),
    ("/v1/missions/{id}/reservations", "get"),
    ("/v1/missions/{id}/observations", "get"),
    ("/v1/missions/{id}/proposals/{proposal_id}/preview", "post"),
    ("/v1/missions/{id}/proposals/{proposal_id}/accept", "post"),
    ("/v1/missions/{id}/proposals/{proposal_id}/decline", "post"),
    # audit and scenario comparison
    ("/v1/audit-events", "get"),
    ("/v1/simulations/{run_id}", "get"),
    ("/v1/simulations/{run_id}/replay-diff", "get"),
    # Phase 4 console: decision trace, replay, scenario comparison, uncertainty
    ("/v1/scenarios/{scenario_id}/compare", "get"),
    ("/v1/missions/{id}/uncertainty", "get"),
}


def test_required_operations_present() -> None:
    present = {(p, m) for p, item in PATHS.items() for m in item if m in {
        "get", "post", "put", "delete", "patch"}}
    missing = REQUIRED_OPERATIONS - present
    assert not missing, f"Missing operations: {sorted(missing)}"


def test_every_route_has_request_and_response_examples() -> None:
    for path, item in PATHS.items():
        for method, op in item.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            assert op.get("responses"), f"{method.upper()} {path} has no responses"
            for _code, resp in op["responses"].items():
                content = resp.get("content")
                if not content:
                    continue
                for media in content.values():
                    schema = media.get("schema", {})
                    ref = schema.get("$ref")
                    assert ref or media.get("example") or "type" in schema

    # Every request body we ship carries an example.
    for name, schema in DOC["components"]["schemas"].items():
        if name.endswith("Request"):
            assert "example" in schema, f"{name} lacks a request example"


def test_conflict_schema_defines_409_body() -> None:
    conflict = DOC["components"]["schemas"]["ConflictResponse"]
    assert "error_code" in conflict["properties"]
    assert "duplicate_commit_conflict" in conflict["properties"]["error_code"]["enum"]


def test_activated_is_simulation_only() -> None:
    mission = DOC["components"]["schemas"]["MissionResponse"]
    assert mission["properties"]["authorized_for_flight"]["description"].lower().startswith(
        "always false"
    )
    assert "activated" in mission["properties"]["status"]["enum"]
