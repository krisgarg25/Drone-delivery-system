"""The real read plane: capability, economics and the seeded comparison, on the wire.

These tests drive the default composition - the production one - through
``TestClient``. They assert three things the console depends on:

1. an observer credential can read simulation results and still cannot write anything;
2. every economics figure arrives with its formula and its denominator, and every
   monetary figure is labelled illustrative;
3. the comparison is the dispatcher's real seeded race over all three arms, and a refused
   race is reported as refused rather than as a result.
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient

from droneops_api.dispatch_plane import (
    ARMS,
    SCENARIO,
    ComparisonEngine,
    ComparisonRunner,
    DispatcherSimulationPort,
    RunOutcome,
    cost_model,
    graph_adjacency,
    lane_geometry,
)
from droneops_api.ports import (
    CAPABILITY_PROPOSAL_ACCEPT,
    CAPABILITY_SIMULATION_READ,
    WRITE_CAPABILITIES,
    ControlPlaneError,
    Principal,
)
from droneops_api.security import ALLOWED_ROLES, ROLE_CAPABILITIES, has_command_role_or_capability
from droneops_dispatcher import MIN_COMPARISON_REPETITIONS, WeakenedBaselineError

NOTICE = "Simulation only. Not authorised for flight."
MONETARY = ("cost_per_completion", "road_baseline")


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ------------------------------------------------------------------ the capability decision


def test_simulation_read_is_a_granted_read_scope_and_no_command_exists() -> None:
    assert "aircraft_commander" not in ALLOWED_ROLES
    assert has_command_role_or_capability() is False
    assert CAPABILITY_SIMULATION_READ not in WRITE_CAPABILITIES
    observer = ROLE_CAPABILITIES["pilot_observer"]
    assert CAPABILITY_SIMULATION_READ in observer
    assert not (observer & set(WRITE_CAPABILITIES)), "the observer scope is read-only by design"
    # A partner that submits orders does not get the operator's economics.
    assert CAPABILITY_SIMULATION_READ not in ROLE_CAPABILITIES["partner_order_writer"]


def test_observer_credential_reads_the_simulation_surface(
    client: TestClient, auth: Callable[..., dict[str, str]]
) -> None:
    run_id = f"{SCENARIO}-policy-a"
    for path in (
        f"/v1/simulations/{run_id}",
        f"/v1/simulations/{run_id}/replay-diff",
        f"/v1/scenarios/{run_id}/compare?against={SCENARIO}-policy-b",
    ):
        response = client.get(path, headers=auth("sim-observer-a"))
        assert response.status_code == 200, f"{path}: {response.text}"
        assert response.json()["simulation_notice"] == NOTICE
        assert response.json()["authoritative"] is False


def test_the_new_grant_gives_read_access_only(
    client: TestClient,
    auth: Callable[..., dict[str, str]],
    config_release: str,
) -> None:
    """The observer can read results and nothing else: no plan, no commit, no accept."""

    assert (
        client.get("/v1/simulations/chandigarh-basic", headers=auth("sim-partner-a")).status_code
        == 403
    )
    body = {
        "idempotency_key": "obs-1",
        "reason_code": "operator_requests_plan",
        "config_release_sha": config_release,
    }
    mission = "00000000-0000-0000-0000-000000000001"
    assert (
        client.post(
            f"/v1/missions/{mission}/plan", json=body, headers=auth("sim-observer-a", "obs-1")
        ).status_code
        == 403
    )
    proposal = "00000000-0000-0000-0000-000000000002"
    denied = client.post(
        f"/v1/missions/{mission}/proposals/{proposal}/accept",
        json={"idempotency_key": "obs-2", "reason_code": "operator_accepts", "note": ""},
        headers=auth("sim-observer-a", "obs-2"),
    )
    assert denied.status_code == 403
    assert CAPABILITY_PROPOSAL_ACCEPT not in ROLE_CAPABILITIES["pilot_observer"]


# ------------------------------------------------------------------ economics on the wire


def test_economics_fields_carry_formula_denominator_and_illustrative_labels(
    client: TestClient,
) -> None:
    response = client.get(f"/v1/simulations/{SCENARIO}-policy-c", headers=auth("sim-observer-a"))
    assert response.status_code == 200, response.text
    payload: dict[str, Any] = response.json()
    assert payload["strategy"] == "hybrid", "the third arm is registered and readable"
    assert payload["repetitions"] >= MIN_COMPARISON_REPETITIONS
    economics = payload["economics"]
    assert economics is not None
    assert economics["illustrative"] is True
    keys = {row["key"] for row in economics["metrics"]}
    for required in (
        "completion_rate",
        "on_time_rate",
        "unserved_rate",
        "detour_ratio",
        "energy_per_completion_wh",
        "pad_wait_us",
        "charger_wait_us",
        "controller_minutes_per_mission",
        "pilot_time_minutes_per_mission",
        "queue_age_median_us",
        "priority_impact_us",
        "planning_latency_median_us",
        "planning_latency_p95_us",
        "cost_per_completion",
        "road_baseline",
    ):
        assert required in keys, f"{required} is not on the wire"
    for row in economics["metrics"]:
        assert row["formula"], row
        assert row["denominator"], row
        assert row["source"], row
        if row["key"] in MONETARY:
            assert row["illustrative"] is True, f"{row['key']} must be labelled illustrative"
    model = economics["cost_model"]
    assert model["illustrative"] is True
    assert "unvalidated" in model["caveat"].lower()
    assert len(model["components"]) == 7
    assert set(model["sensitivity_inr_per_drop"]) == {"50", "150", "300"}
    assert (
        economics["unserved_at_cutoff"] == economics["rejected"] + economics["unfinished_at_cutoff"]
    )
    assert economics["charge_states"], "aircraft charge state travels with the run"
    for state in economics["charge_states"]:
        assert 0 <= state["charge_bp"] <= 10_000


def test_cost_model_is_the_chapter_seven_arithmetic_and_is_labelled() -> None:
    model = cost_model(150, 100)
    assert model.illustrative is True
    assert model.cost_per_completed_delivery_inr == 23_700_000 // (150 * 300) + 215
    assert model.sensitivity_inr_per_drop["300"] < model.sensitivity_inr_per_drop["150"]
    assert model.road_baseline_inr_low < model.road_baseline_inr_high


# ------------------------------------------------------------------ the comparison itself


def test_comparison_reports_all_three_lanes_with_median_and_spread(client: TestClient) -> None:
    response = client.get(
        f"/v1/scenarios/{SCENARIO}-policy-a/compare?against={SCENARIO}-policy-b",
        headers=auth("sim-observer-a"),
    )
    assert response.status_code == 200, response.text
    payload: dict[str, Any] = response.json()
    assert payload["comparison_error"] == ""
    assert len(payload["seeds"]) >= MIN_COMPARISON_REPETITIONS
    assert payload["repetitions"] == len(payload["seeds"])
    assert {row["strategy"] for row in payload["lanes"]} == {name for _, name in ARMS}
    assert len(payload["lanes"]) == len(ARMS), "the hybrid lane is registered as a real run"
    for row in payload["lanes"]:
        assert row["repetitions"] == len(payload["seeds"])
        assert row["spread_completed"] >= 0
        assert row["economics"]["cost_model"]["illustrative"] is True
    assert payload["fairness_checks"], "the report carries its proof that the race was fair"
    for metric in (
        "pad_wait_us",
        "charger_wait_us",
        "energy_per_completion_wh",
        "cost_per_completion",
    ):
        assert payload["compared_metrics"][metric], metric
    assert "illustrative" in payload["compared_metrics"]["cost_per_completion"]
    assert "median_spread_completed" in payload["compared_metrics"]


def test_a_weakened_baseline_is_a_failed_comparison_not_a_silent_result() -> None:
    class RiggedRunner(ComparisonRunner):
        def run(self, strategy: str, *, seed: int) -> RunOutcome:
            outcome = super().run(strategy, seed=seed)
            if strategy == "hybrid":
                return RunOutcome(**{**_as_dict(outcome), "pad_capacity": outcome.pad_capacity - 1})
            return outcome

    engine = ComparisonEngine(RiggedRunner(order_count=2))
    port = DispatcherSimulationPort(engine)
    principal = _principal()
    with pytest.raises((ControlPlaneError, WeakenedBaselineError)) as raised:
        port.run(principal, f"{SCENARIO}-policy-a")
    assert "Weakened" in str(raised.value) or "comparison_failed" in str(raised.value)
    comparison = port.compare(principal, f"{SCENARIO}-policy-a", f"{SCENARIO}-policy-b")
    assert comparison.verdict.startswith("comparison_failed")
    assert comparison.comparison_error
    assert "WeakenedBaselineError" in comparison.comparison_error
    assert comparison.compared_metrics == {}


def _as_dict(outcome: RunOutcome) -> dict[str, object]:
    loaded: dict[str, object] = dataclasses.asdict(outcome)
    return loaded


def _principal() -> Principal:


    return Principal(
        tenant_id=uuid.uuid4(),
        actor_id="actor-test",
        actor_type="user",
        roles=("scheduler_operator",),
        capabilities=frozenset({CAPABILITY_SIMULATION_READ}),
    )


# ------------------------------------------------------------------ real corridor geometry


def test_route_geometry_and_adjacency_come_from_the_corridor_graph() -> None:
    edges = lane_geometry()
    assert edges, "the corridor graph declares no lanes"
    adjacency = graph_adjacency()
    assert adjacency
    for edge in edges:
        assert int(edge["length_mm"]) > 0  # type: ignore[arg-type]
        assert edge["vertices"], edge
    assert any(neighbours for neighbours in adjacency.values()), (
        "no lane in the real graph has a neighbour"
    )
