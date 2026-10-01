"""Access control: tenancy, capability scopes and the absence of a command role.

``technical-build/06`` section 8, as executable code. A cross-tenant read is answered with
the same neutral body as a read of something that does not exist at all, so probing cannot
discover another tenant's rows; a read-only credential cannot accept a proposal; a
scheduler cannot change a maintenance record because no such operation exists; an auditor
reads and never writes; and no role in this system is named ``aircraft_commander``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi.testclient import TestClient

from droneops_api.composition import SIMULATION_CREDENTIALS
from droneops_api.ports import ALL_CAPABILITIES
from droneops_api.security import ROLE_CAPABILITIES, has_command_role_or_capability
from droneops_contracts import OperatorRole

TENANT_A = "22222222-2222-2222-2222-222222222222"
TENANT_B = "33333333-3333-3333-3333-333333333333"

UNKNOWN_ORDER = "00000000-0000-0000-0000-0000000000ff"


def test_no_role_or_capability_names_aircraft_command() -> None:
    assert "aircraft_commander" not in {role.value for role in OperatorRole}
    assert "aircraft_commander" not in ROLE_CAPABILITIES
    assert has_command_role_or_capability() is False
    for _token, credential in SIMULATION_CREDENTIALS:
        assert "aircraft_commander" not in credential.roles
        assert not any("command" in capability for capability in credential.capabilities)


def test_cross_tenant_read_is_neutral_and_does_not_leak_existence(
    client: TestClient,
    auth: Callable[..., dict[str, str]],
    create_order: Callable[..., dict[str, Any]],
) -> None:
    order = create_order("k-tenant")
    mine = client.get(f"/v1/orders/{order['order_id']}", headers=auth("sim-scheduler-a"))
    assert mine.status_code == 200

    theirs = client.get(f"/v1/orders/{order['order_id']}", headers=auth("sim-scheduler-b"))
    nonexistent = client.get(f"/v1/orders/{UNKNOWN_ORDER}", headers=auth("sim-scheduler-b"))
    assert theirs.status_code == 404
    assert nonexistent.status_code == 404
    assert {k: v for k, v in theirs.json().items() if k != "request_id"} == {
        k: v for k, v in nonexistent.json().items() if k != "request_id"
    }
    assert TENANT_A not in theirs.text
    assert order["mission_id"] not in theirs.text


def test_cross_tenant_mission_decision_is_not_found(
    client: TestClient,
    auth: Callable[..., dict[str, str]],
    create_order: Callable[..., dict[str, Any]],
    plan: Callable[..., dict[str, Any]],
) -> None:
    mission_id = str(create_order("k-trace-tenant")["mission_id"])
    plan(mission_id, "k-trace-tenant-plan")
    response = client.get(f"/v1/missions/{mission_id}/decision", headers=auth("sim-scheduler-b"))
    assert response.status_code == 404
    assert response.json()["error_code"] == "tenant_not_authorized"


def test_cross_tenant_fleet_and_facility_reads_return_nothing(
    client: TestClient,
    auth: Callable[..., dict[str, str]],
    create_order: Callable[..., dict[str, Any]],
) -> None:
    order = create_order("k-fleet")
    aircraft = client.get("/v1/aircraft", headers=auth("sim-scheduler-b")).json()["items"]
    assert aircraft and all(item["tenant_id"] == TENANT_B for item in aircraft)  # noqa: PT018
    facilities = client.get("/v1/facilities", headers=auth("sim-scheduler-b")).json()["items"]
    assert all(item["tenant_id"] == TENANT_B for item in facilities)
    assert (
        client.get(f"/v1/orders/{order['order_id']}", headers=auth("sim-scheduler-b")).status_code
        == 404
    )


def test_read_only_credential_cannot_accept_a_proposal(
    client: TestClient,
    auth: Callable[..., dict[str, str]],
    create_order: Callable[..., dict[str, Any]],
    plan: Callable[..., dict[str, Any]],
) -> None:
    mission_id = str(create_order("k-readonly")["mission_id"])
    plan(mission_id, "k-readonly-plan")

    denied = client.post(
        f"/v1/missions/{mission_id}/proposals/{UNKNOWN_ORDER}/accept",
        json={"reason_code": "observer_tries_to_accept", "idempotency_key": "ro-1"},
        headers=auth("sim-observer-a", "ro-1"),
    )
    assert denied.status_code == 403
    assert denied.json()["error_code"] == "tenant_not_authorized"
    assert "proposal:accept" in denied.json()["message"]
    assert client.get("/v1/aircraft", headers=auth("sim-observer-a")).status_code == 200


def test_auditor_can_read_but_not_write(
    client: TestClient, auth: Callable[..., dict[str, str]], committed: dict[str, Any]
) -> None:
    assert client.get("/v1/audit-events", headers=auth("sim-auditor-a")).status_code == 200
    assert client.get("/v1/reservations", headers=auth("sim-auditor-a")).status_code == 200
    denied = client.post(
        "/v1/orders",
        json={
            "external_ref": "x",
            "origin_facility_id": "hub-north",
            "destination_facility_id": "node-hospital-a",
            "payload_profile_id": "kit",
            "priority_class": "routine",
            "earliest_departure": "2026-10-01T09:00:00Z",
            "requested_deadline": "2026-10-01T09:20:00Z",
            "idempotency_key": "aud-1",
        },
        headers=auth("sim-auditor-a", "aud-1"),
    )
    assert denied.status_code == 403
    assert committed["reservations"]


def test_scheduler_cannot_change_a_maintenance_record(
    client: TestClient, auth: Callable[..., dict[str, str]]
) -> None:
    """There is nothing to call: no aircraft write route exists at any scope."""

    aircraft = client.get("/v1/aircraft", headers=auth("sim-scheduler-a")).json()["items"]
    target = next(item for item in aircraft if item["status"] == "maintenance")
    for method in ("post", "patch", "put", "delete"):
        response = client.request(
            method.upper(),
            f"/v1/aircraft/{target['aircraft_id']}/maintenance",
            json={"reason_code": "clear_the_record", "idempotency_key": "m-1"},
            headers=auth("sim-scheduler-a", "m-1"),
        )
        assert response.status_code == 404, f"{method} reached a fleet write route"
    assert "maintenance" not in {capability.split(":")[0] for capability in ALL_CAPABILITIES}
    listed = client.get("/v1/aircraft", headers=auth("sim-scheduler-a")).json()["items"]
    assert next(item for item in listed if item["aircraft_id"] == target["aircraft_id"])[
        "status"
    ] == ("maintenance")


def test_partner_cannot_read_the_fleet_or_commit_a_plan(
    client: TestClient,
    auth: Callable[..., dict[str, str]],
    create_order: Callable[..., dict[str, Any]],
    config_release: str,
) -> None:
    assert client.get("/v1/aircraft", headers=auth("sim-partner-a")).status_code == 403
    mission_id = str(create_order("k-partner")["mission_id"])
    denied = client.post(
        f"/v1/missions/{mission_id}/reserve",
        json={
            "plan_version_id": UNKNOWN_ORDER,
            "config_release_sha": config_release,
            "idempotency_key": "pt-1",
        },
        headers=auth("sim-partner-a", "pt-1"),
    )
    assert denied.status_code == 403


def test_safety_reviewer_may_decline_but_not_commit(
    client: TestClient,
    auth: Callable[..., dict[str, str]],
    create_order: Callable[..., dict[str, Any]],
    config_release: str,
) -> None:
    mission_id = str(create_order("k-safety")["mission_id"])
    assert (
        client.post(
            f"/v1/missions/{mission_id}/reserve",
            json={
                "plan_version_id": UNKNOWN_ORDER,
                "config_release_sha": config_release,
                "idempotency_key": "sf-1",
            },
            headers=auth("sim-safety-a", "sf-1"),
        ).status_code
        == 403
    )
    # declining a proposal they cannot even preview is refused for the same reason
    assert (
        client.post(
            f"/v1/missions/{mission_id}/proposals/{UNKNOWN_ORDER}/accept",
            json={"reason_code": "not_mine_to_accept", "idempotency_key": "sf-2"},
            headers=auth("sim-safety-a", "sf-2"),
        ).status_code
        == 403
    )


def test_missing_and_unknown_credentials_are_rejected(
    client: TestClient, auth: Callable[..., dict[str, str]]
) -> None:
    assert client.get("/v1/aircraft").status_code == 401
    assert client.get("/v1/aircraft", headers=auth("not-a-credential")).status_code == 401
    assert client.get(
        "/v1/aircraft", headers={"X-DroneOps-Credential": "sim-auditor-a"}
    ).status_code == (200)


def test_every_write_records_actor_reason_and_configuration_release(  # noqa: PLR0917 - pytest fixtures arrive as parameters
    client: TestClient,
    auth: Callable[..., dict[str, str]],
    create_order: Callable[..., dict[str, Any]],
    plan: Callable[..., dict[str, Any]],
    committed: dict[str, Any],
    config_release: str,
) -> None:
    mission_id = str(create_order("k-write-audit")["mission_id"])
    facilities = client.get("/v1/facilities", headers=auth("sim-scheduler-a")).json()["items"]
    hub = next(item for item in facilities if item["name"].startswith("hub"))
    client.post(
        f"/v1/facilities/{hub['facility_id']}/outage",
        json={
            "reason_code": "pad_closed_for_survey",
            "valid_from": "2026-10-04T08:00:00Z",
            "valid_until": "2026-10-04T11:00:00Z",
            "idempotency_key": "w-1",
        },
        headers=auth("sim-scheduler-a", "w-1"),
    )
    client.post(
        f"/v1/missions/{mission_id}/cancel",
        json={
            "reason_code": "operator_stops_dispatch",
            "note": "see trace",
            "idempotency_key": "w-2",
        },
        headers=auth("sim-scheduler-a", "w-2"),
    )
    audit = client.get("/v1/audit-events", headers=auth("sim-auditor-a")).json()["items"]
    by_kind = {event["operation_kind"]: event for event in audit}
    for kind in (
        "order_received",
        "plan_version_generated",
        "reservation_committed",
        "facility_outage_recorded",
        "mission_cancelled",
    ):
        assert kind in by_kind, f"{kind} was not audited"
        event = by_kind[kind]
        assert event["actor_id"].startswith("actor-")
        assert event["reason_code"]
        assert event["config_release_sha"] == config_release
