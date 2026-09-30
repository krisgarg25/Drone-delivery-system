from fastapi.testclient import TestClient

from droneops_api.main import app


def make_client() -> TestClient:
    app.state.missions.clear()
    return TestClient(app)


def test_health_declares_simulation_only() -> None:
    client = make_client()
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["authorized_for_flight"] is False
    assert body["phase"] == "phase-1-simulation"


def test_openapi_has_no_aircraft_command_operations() -> None:
    client = make_client()
    schema = client.get("/openapi.json").json()
    operations = []
    for path, methods in schema["paths"].items():
        for method, operation in methods.items():
            operations.append((method.upper(), path, operation.get("operationId", "")))

    forbidden_terms = (
        "command",
        "launch",
        "start",
        "stop",
        "land",
        "takeoff",
        "rtl",
        "return",
        "upload",
        "parameter",
        "firmware",
        "mav_cmd",
    )
    joined = " ".join(method + path + operation_id for method, path, operation_id in operations).lower()
    for term in forbidden_terms:
        assert term not in joined, f"Unexpected command-related operation: {term}"


def test_simulated_order_is_idempotent() -> None:
    client = make_client()
    payload = {
        "origin_facility_id": "hub-north-synthetic",
        "destination_facility_id": "node-hospital-a-synthetic",
        "priority_class": "routine",
        "altitude_reference": "AGL",
        "idempotency_key": "request-1",
    }
    first = client.post("/v1/simulated-orders", json=payload, headers={"Idempotency-Key": "request-1"})
    second = client.post("/v1/simulated-orders", json=payload, headers={"Idempotency-Key": "request-1"})
    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json()["mission_id"] == second.json()["mission_id"]
    assert first.json()["authorized_for_flight"] is False
