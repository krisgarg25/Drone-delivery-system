"""Fixtures for the Phase 4 control-plane API tests.

Every test drives the documented surface through ``TestClient`` against the composition
root, so the evidence is the wire contract rather than an internal call.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient

from droneops_api.composition import create_app

ORIGIN = "hub-north"
DESTINATION = "node-hospital-a"
OTHER_DESTINATION = "node-clinic-b"
NOTICE = "Simulation only. Not authorised for flight."


def order_body(key: str, *, destination: str = DESTINATION, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "external_ref": f"partner-{key}",
        "origin_facility_id": ORIGIN,
        "destination_facility_id": destination,
        "payload_profile_id": "diagnostic-kit-v1",
        "priority_class": "routine",
        "earliest_departure": "2026-10-01T09:00:00Z",
        "requested_deadline": "2026-10-01T09:20:00Z",
        "idempotency_key": key,
    }
    body.update(overrides)
    return body


@pytest.fixture(name="client")
def client_fixture() -> TestClient:
    return TestClient(create_app(), raise_server_exceptions=False)


@pytest.fixture(name="auth")
def auth_fixture() -> Callable[..., dict[str, str]]:
    def _headers(token: str, idempotency_key: str | None = None, **extra: str) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {token}"}
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        headers.update(extra)
        return headers

    return _headers


@pytest.fixture(name="config_release")
def config_release_fixture(client: TestClient) -> str:
    response = client.get("/version")
    assert response.status_code == 200
    return str(response.json()["config_release_sha"])


@pytest.fixture(name="create_order")
def create_order_fixture(
    client: TestClient, auth: Callable[..., dict[str, str]]
) -> Callable[..., dict[str, Any]]:
    def _create(key: str = "k-order", **overrides: Any) -> dict[str, Any]:
        response = client.post(
            "/v1/orders", json=order_body(key, **overrides), headers=auth("sim-partner-a", key)
        )
        assert response.status_code == 201, response.text
        return response.json()

    return _create


@pytest.fixture(name="plan")
def plan_fixture(
    client: TestClient, auth: Callable[..., dict[str, str]], config_release: str
) -> Callable[..., dict[str, Any]]:
    def _plan(mission_id: str, key: str = "k-plan") -> dict[str, Any]:
        response = client.post(
            f"/v1/missions/{mission_id}/plan",
            json={
                "config_release_sha": config_release,
                "idempotency_key": key,
                "reason_code": "operator_requests_plan",
            },
            headers=auth("sim-scheduler-a", key),
        )
        assert response.status_code == 202, response.text
        return response.json()

    return _plan


@pytest.fixture(name="reserve")
def reserve_fixture(
    client: TestClient, auth: Callable[..., dict[str, str]], config_release: str
) -> Callable[..., Any]:
    def _reserve(mission_id: str, plan_version_id: str, key: str = "k-reserve") -> Any:
        return client.post(
            f"/v1/missions/{mission_id}/reserve",
            json={
                "plan_version_id": plan_version_id,
                "config_release_sha": config_release,
                "idempotency_key": key,
                "reason_code": "operator_commits_plan",
            },
            headers=auth("sim-scheduler-a", key),
        )

    return _reserve


@pytest.fixture(name="committed")
def committed_fixture(
    create_order: Callable[..., dict[str, Any]],
    plan: Callable[..., dict[str, Any]],
    reserve: Callable[..., Any],
) -> dict[str, Any]:
    """An order that has been planned and committed: the committed reference case."""

    order = create_order("k-committed")
    mission_id = str(order["mission_id"])
    versions = plan(mission_id, "k-committed-plan")["items"]
    response = reserve(mission_id, str(versions[0]["plan_version_id"]), "k-committed-reserve")
    assert response.status_code == 201, response.text
    return {
        "order": order,
        "mission_id": mission_id,
        "plan_version_ids": [str(item["plan_version_id"]) for item in versions],
        "reservations": response.json()["items"],
    }
