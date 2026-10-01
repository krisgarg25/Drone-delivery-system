"""Idempotency, the provenance envelope and agreement with the published contract.

Conventions section 8 and ``technical-build/04`` section 11: every ``POST`` carries a
client key in the header *and* the body, a retry after a commit returns the original
result, and a conflicting duplicate returns 409 - never a second commitment.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml
from fastapi.testclient import TestClient

NOTICE = "Simulation only. Not authorised for flight."
CONTRACT = Path("contracts/openapi/v1.yaml")


def test_retry_of_an_order_returns_the_original_and_creates_no_second_mission(
    client: TestClient, auth: Callable[..., dict[str, str]]
) -> None:
    body = {
        "external_ref": "partner-dup",
        "origin_facility_id": "hub-north",
        "destination_facility_id": "node-hospital-a",
        "payload_profile_id": "diagnostic-kit-v1",
        "priority_class": "routine",
        "earliest_departure": "2026-10-01T09:00:00Z",
        "requested_deadline": "2026-10-01T09:20:00Z",
        "idempotency_key": "dup-1",
    }
    first = client.post("/v1/orders", json=body, headers=auth("sim-partner-a", "dup-1"))
    second = client.post("/v1/orders", json=body, headers=auth("sim-partner-a", "dup-1"))
    assert first.status_code == 201
    assert second.status_code == 200, "a replay is returned as a replay"
    assert first.json()["mission_id"] == second.json()["mission_id"]
    assert first.json()["order_id"] == second.json()["order_id"]
    listed = client.get("/v1/orders", headers=auth("sim-partner-a")).json()["items"]
    assert sum(1 for item in listed if item["order_id"] == first.json()["order_id"]) == 1


def test_same_key_with_a_different_body_is_a_conflict(
    client: TestClient, auth: Callable[..., dict[str, str]]
) -> None:
    body = {
        "external_ref": "partner-a",
        "origin_facility_id": "hub-north",
        "destination_facility_id": "node-hospital-a",
        "payload_profile_id": "diagnostic-kit-v1",
        "priority_class": "routine",
        "earliest_departure": "2026-10-01T09:00:00Z",
        "requested_deadline": "2026-10-01T09:20:00Z",
        "idempotency_key": "reuse-1",
    }
    assert (
        client.post("/v1/orders", json=body, headers=auth("sim-partner-a", "reuse-1")).status_code
        == 201
    )
    changed = dict(body, external_ref="partner-b")
    conflict = client.post("/v1/orders", json=changed, headers=auth("sim-partner-a", "reuse-1"))
    assert conflict.status_code == 409
    assert conflict.json()["error_code"] == "idempotency_key_reused"


def test_header_and_body_keys_must_agree(
    client: TestClient, auth: Callable[..., dict[str, str]]
) -> None:
    body = {
        "external_ref": "partner-mismatch",
        "origin_facility_id": "hub-north",
        "destination_facility_id": "node-hospital-a",
        "payload_profile_id": "diagnostic-kit-v1",
        "priority_class": "routine",
        "earliest_departure": "2026-10-01T09:00:00Z",
        "requested_deadline": "2026-10-01T09:20:00Z",
        "idempotency_key": "in-body",
    }
    mismatch = client.post("/v1/orders", json=body, headers=auth("sim-partner-a", "in-header"))
    assert mismatch.status_code == 422
    assert mismatch.json()["error_code"] == "validation_error"
    without_header = client.post("/v1/orders", json=body, headers=auth("sim-partner-a"))
    assert without_header.status_code == 422, "Idempotency-Key header is required on every POST"


def test_retry_after_commit_never_produces_a_second_commitment(  # noqa: PLR0917 - pytest fixtures arrive as parameters
    client: TestClient,
    auth: Callable[..., dict[str, str]],
    config_release: str,
    create_order: Callable[..., dict[str, Any]],
    plan: Callable[..., dict[str, Any]],
    reserve: Callable[..., Any],
) -> None:
    mission_id = str(create_order("k-idem")["mission_id"])
    version = plan(mission_id, "k-idem-plan")["items"][0]["plan_version_id"]
    first = reserve(mission_id, str(version), "k-idem-reserve")
    assert first.status_code == 201
    replay = reserve(mission_id, str(version), "k-idem-reserve")
    assert replay.status_code == 200
    assert replay.json()["items"] == first.json()["items"]

    # a fresh key on an already committed mission replays the commitment; it never doubles it
    second_attempt = reserve(mission_id, str(version), "k-idem-reserve-2")
    assert second_attempt.status_code in (200, 201)
    assert second_attempt.json()["items"] == first.json()["items"]
    other = plan(mission_id, "k-idem-plan-2")["items"]
    if len(other) > 1:
        rival = reserve(mission_id, str(other[1]["plan_version_id"]), "k-idem-rival")
        assert rival.status_code == 409
        assert rival.json()["error_code"] == "duplicate_commit_conflict"
        assert rival.json()["existing_resource_id"]
    stored = client.get(f"/v1/missions/{mission_id}/reservations", headers=auth("sim-scheduler-a"))
    assert stored.status_code == 200
    assert stored.json()["items"] == first.json()["items"]


def test_malformed_input_is_a_documented_422_never_a_500(
    client: TestClient, auth: Callable[..., dict[str, str]]
) -> None:
    cases = [
        {"external_ref": "x"},
        {
            "external_ref": "x",
            "origin_facility_id": "a",
            "destination_facility_id": "b",
            "payload_profile_id": "p",
            "priority_class": "not-a-priority",
            "earliest_departure": "2026-10-01T09:00:00Z",
            "requested_deadline": "2026-10-01T09:20:00Z",
            "idempotency_key": "bad-1",
        },
        {
            "external_ref": "x",
            "origin_facility_id": "a",
            "destination_facility_id": "b",
            "payload_profile_id": "p",
            "priority_class": "routine",
            "earliest_departure": "2026-10-01T09:00:00",
            "requested_deadline": "2026-10-01T09:20:00Z",
            "idempotency_key": "bad-2",
        },
        {
            "external_ref": "x",
            "origin_facility_id": "a",
            "destination_facility_id": "b",
            "payload_profile_id": "p",
            "priority_class": "routine",
            "earliest_departure": "2026-10-01T09:00:00Z",
            "requested_deadline": "2026-10-01T08:00:00Z",
            "idempotency_key": "bad-3",
        },
    ]
    for index, body in enumerate(cases, start=1):
        response = client.post(
            "/v1/orders", json=body, headers=auth("sim-partner-a", f"bad-{index}")
        )
        assert response.status_code == 422, response.text
        assert response.json()["error_code"] in {"validation_error", "non_utc_timestamp"}
        assert response.json()["simulation_notice"] == NOTICE


def test_every_documented_response_carries_the_provenance_envelope(
    client: TestClient,
    auth: Callable[..., dict[str, str]],
    committed: dict[str, Any],
    create_order: Callable[..., dict[str, Any]],
) -> None:
    mission_id = committed["mission_id"]
    order_id = committed["order"]["order_id"]
    facility = client.get("/v1/facilities", headers=auth("sim-scheduler-a")).json()["items"][0]
    aircraft = client.get("/v1/aircraft", headers=auth("sim-scheduler-a")).json()["items"][0]
    reads: list[tuple[str, dict[str, str]]] = [
        (f"/v1/orders/{order_id}", {"Authorization": "Bearer sim-scheduler-a"}),
        ("/v1/orders", {"Authorization": "Bearer sim-scheduler-a"}),
        (f"/v1/missions/{mission_id}/plan-versions", {"Authorization": "Bearer sim-scheduler-a"}),
        (f"/v1/missions/{mission_id}/decision", {"Authorization": "Bearer sim-scheduler-a"}),
        (f"/v1/missions/{mission_id}/reservations", {"Authorization": "Bearer sim-scheduler-a"}),
        (f"/v1/missions/{mission_id}/observations", {"Authorization": "Bearer sim-scheduler-a"}),
        (f"/v1/missions/{mission_id}/uncertainty", {"Authorization": "Bearer sim-scheduler-a"}),
        (f"/v1/aircraft/{aircraft['aircraft_id']}", {"Authorization": "Bearer sim-scheduler-a"}),
        (
            f"/v1/facilities/{facility['facility_id']}/capacity",
            {"Authorization": "Bearer sim-scheduler-a"},
        ),
        ("/v1/reservations", {"Authorization": "Bearer sim-scheduler-a"}),
        ("/v1/audit-events", {"Authorization": "Bearer sim-auditor-a"}),
        (
            "/v1/simulations/run-chandigarh-synthetic-v1-policy-a",
            {"Authorization": "Bearer sim-admin-a"},
        ),
        (
            "/v1/simulations/run-chandigarh-synthetic-v1-policy-a/replay-diff",
            {"Authorization": "Bearer sim-admin-a"},
        ),
        (
            "/v1/scenarios/run-chandigarh-synthetic-v1-policy-a/compare?against"
            "=run-chandigarh-synthetic-v1-policy-b",
            {"Authorization": "Bearer sim-admin-a"},
        ),
    ]
    envelope = {"phase", "source_kind", "authoritative", "simulation_notice"}
    for path, headers in reads:
        response = client.get(path, headers=headers)
        assert response.status_code == 200, f"{path}: {response.text}"
        body: dict[str, Any] = response.json()
        assert envelope <= set(body), path
        assert body["simulation_notice"] == NOTICE
        assert body["source_kind"] in {"planned", "committed", "observed", "synthetic"}
        for item in body.get("items", []):
            assert envelope <= set(item), path
            assert item["simulation_notice"] == NOTICE
    assert client.get("/health").json()["authorized_for_flight"] is False


def test_app_surface_matches_the_published_contract_exactly(client: TestClient) -> None:
    document = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    methods = {"get", "post", "put", "patch", "delete"}
    declared = {
        (method.upper(), path)
        for path, item in document["paths"].items()
        for method in item
        if method in methods
    }
    spec = client.get("/openapi.json").json()
    exposed = {
        (method.upper(), path)
        for path, item in spec["paths"].items()
        for method in item
        if method in methods
    }
    operational = {"/health", "/version", "/docs", "/openapi.json", "/redoc"}
    legacy = {("POST", "/v1/simulated-orders")}
    business = {pair for pair in exposed if pair[1] not in operational}
    assert business - legacy == declared, (business - legacy) ^ declared


def test_contract_declares_provenance_on_every_response_schema() -> None:
    document = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    envelope = {"phase", "source_kind", "authoritative", "simulation_notice"}
    for name, schema in document["components"]["schemas"].items():
        if "Response" not in name or "Request" in name:
            continue
        assert envelope <= set(schema.get("required", [])), f"{name} lacks provenance"
        assert envelope <= set(schema.get("properties", {})), f"{name} lacks provenance"
    for name in (
        "PlanRequest",
        "ReserveRequest",
        "OutageRequest",
        "OrderRequest",
        "CancelRequest",
        "ProposalDecisionRequest",
    ):
        assert "idempotency_key" in document["components"]["schemas"][name]["properties"], name


def test_contract_decision_trace_carries_the_operator_facing_explanation() -> None:
    document = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    trace = document["components"]["schemas"]["DecisionTraceResponse"]["properties"]
    assert {"inputs", "eligible", "ranking", "rejected", "primary_human_reason"} <= set(trace)
    rejected = document["components"]["schemas"]["RejectedCandidate"]["properties"]
    assert "human_reason" in rejected and "technical_detail" in rejected  # noqa: PT018
    assert document["paths"]["/v1/status/stream"]["get"]["operationId"] == "readStatusStream"
