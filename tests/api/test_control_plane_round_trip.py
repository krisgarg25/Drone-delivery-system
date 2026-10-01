"""The live operator journey: order, plan, commit, explain a rejection.

This is the Phase 4 acceptance path from ``technical-build/05``: a person submits an
order, sees which candidates survived and which were refused, reads *why* without opening
a database, commits a plan, and can then cancel. Every assertion is on the wire.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from itertools import pairwise
from typing import Any

from fastapi.testclient import TestClient

NOTICE = "Simulation only. Not authorised for flight."


def test_create_order_returns_committed_record_and_provenance(
    create_order: Callable[..., dict[str, Any]],
) -> None:
    order = create_order("k-flow")
    assert order["status"] == "received"
    assert order["source_kind"] == "committed"
    assert order["authoritative"] is True
    assert order["phase"] == "phase-4-simulation"
    assert order["simulation_notice"] == NOTICE
    assert order["tenant_id"] == "22222222-2222-2222-2222-222222222222"
    assert order["mission_id"] != order["order_id"]


def test_plan_is_a_proposal_and_never_authoritative(
    create_order: Callable[..., dict[str, Any]], plan: Callable[..., dict[str, Any]]
) -> None:
    mission_id = str(create_order("k-plan")["mission_id"])
    body = plan(mission_id)
    assert body["source_kind"] == "planned"
    assert body["authoritative"] is False
    assert body["simulation_notice"] == NOTICE
    items = body["items"]
    assert items, "a detour strategy survives the restricted segment"
    assert all(item["source_kind"] == "planned" for item in items)
    assert all(item["authoritative"] is False for item in items)
    assert all(item["strategy"] == "restricted-avoidance" for item in items)
    assert {item["mission_id"] for item in items} == {mission_id}


def test_reserve_commits_the_ledger_and_marks_every_resource_committed(
    committed: dict[str, Any],
) -> None:
    reservations = committed["reservations"]
    assert reservations
    for item in reservations:
        assert item["state"] == "committed"
        assert item["authoritative"] is True
        assert item["source_kind"] == "committed"
        assert item["simulation_notice"] == NOTICE
        assert item["resource_key"].startswith(("corridor:", "pad:", "charger_bay:"))
    kinds = {item["resource_kind"] for item in reservations}
    assert {"corridor", "pad", "charger_bay"} <= kinds
    assert len({item["reservation_id"] for item in reservations}) == len(reservations)


def test_decision_endpoint_serves_the_stored_trace_readable_in_sixty_seconds(
    client: TestClient,
    auth: Callable[..., dict[str, str]],
    create_order: Callable[..., dict[str, Any]],
    plan: Callable[..., dict[str, Any]],
) -> None:
    mission_id = str(create_order("k-trace")["mission_id"])
    plan(mission_id, "k-trace-plan")
    response = client.get(f"/v1/missions/{mission_id}/decision", headers=auth("sim-scheduler-a"))
    assert response.status_code == 200
    trace = response.json()

    # the eleven numbered steps of the commit transaction, in order
    assert [step["step"] for step in trace["steps"]] == list(range(1, 12))
    assert trace["record"]["config_release_sha"].startswith("sha256:")
    assert trace["record"]["reason_codes"]

    # inputs, eligible aircraft, candidate ranking, rejections - all present
    inputs = trace["inputs"]
    assert inputs["snapshot_digest"] and inputs["reservation_version"] >= 0  # noqa: PT018
    assert trace["eligible"] and trace["ranking"]  # noqa: PT018
    assert trace["rejected"], "the maintenance and lost-link aircraft are reported, not hidden"

    # the cause of a rejection is the first thing an operator reads
    first = trace["rejected"][0]
    assert first["human_reason"] and first["technical_detail"]  # noqa: PT018
    assert first["reason_code"].startswith(("ledger.", "checker."))
    assert trace["primary_human_reason"] == first["human_reason"]
    assert trace["primary_reason_code"] == first["reason_code"]
    assert trace["explanation_readable_in_s"] == 60

    # the response is the stored explanation, so it is never authoritative as a commitment
    assert trace["record"]["committed"] is False
    assert trace["source_kind"] == "planned"
    assert trace["simulation_notice"] == NOTICE


def test_a_rejection_explains_the_restricted_corridor_in_technical_terms(
    client: TestClient,
    auth: Callable[..., dict[str, str]],
    create_order: Callable[..., dict[str, Any]],
    plan: Callable[..., dict[str, Any]],
    committed: dict[str, Any],
) -> None:
    """Second order on the same corridor: the committed reservation is the reason."""

    second = create_order("k-blocked", destination="node-hospital-a")
    mission_id = str(second["mission_id"])
    body = plan(mission_id, "k-blocked-plan")
    assert body["items"] == []
    trace = client.get(
        f"/v1/missions/{mission_id}/decision", headers=auth("sim-scheduler-a")
    ).json()
    assert trace["primary_reason_code"] == "ledger.committed_overlap"
    assert any("already committed" in item["technical_detail"] for item in trace["rejected"])
    assert trace["primary_human_reason"]


def test_observations_are_observed_and_never_authoritative(
    client: TestClient, auth: Callable[..., dict[str, str]], committed: dict[str, Any]
) -> None:
    mission_id = committed["mission_id"]
    body = client.get(
        f"/v1/missions/{mission_id}/observations", headers=auth("sim-scheduler-a")
    ).json()
    items = body["items"]
    assert items and all(item["source_kind"] == "observed" for item in items)  # noqa: PT018
    assert all(item["authoritative"] is False for item in items)
    assert any(item["out_of_order"] for item in items), "a late frame is labelled, not smoothed"
    newest = max(item["measurement_time"] for item in items if not item["out_of_order"])
    late = [item for item in items if item["out_of_order"]]
    assert all(item["measurement_time"] < newest for item in late)
    assert all(item["position"]["altitude_reference"] == "AGL" for item in items)


def test_uncertainty_retains_the_protected_volume(
    client: TestClient, auth: Callable[..., dict[str, str]], committed: dict[str, Any]
) -> None:
    mission_id = committed["mission_id"]
    state = client.get(
        f"/v1/missions/{mission_id}/uncertainty", headers=auth("sim-scheduler-a")
    ).json()
    assert state["state"] in {"uncertain", "lost"}
    assert state["protected_volume_retained"] is True
    assert state["staleness_s"] > 0
    assert state["source_kind"] == "observed"


def test_status_stream_is_event_stream_and_labels_each_event(
    client: TestClient, auth: Callable[..., dict[str, str]], committed: dict[str, Any]
) -> None:
    response = client.get("/v1/status/stream?limit=200", headers=auth("sim-scheduler-a"))
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    payloads = [
        json.loads(line[6:])
        for line in response.text.splitlines()
        if line.startswith("data: ") and line[6:].startswith("{")
    ]
    assert payloads, "the stream carries the committed reservations"
    kinds = {event["kind"] for event in payloads}
    assert {"order.received", "plan.generated", "reservation.committed"} <= kinds
    for event in payloads:
        assert event["simulation_notice"] == NOTICE
        assert event["source_kind"] in {"planned", "committed", "observed", "synthetic"}
        assert event["authoritative"] is (event["source_kind"] == "committed")


def test_proposal_accept_mutates_the_workflow_record_only(
    client: TestClient,
    auth: Callable[..., dict[str, str]],
    create_order: Callable[..., dict[str, Any]],
    plan: Callable[..., dict[str, Any]],
) -> None:
    mission_id = str(create_order("k-proposal")["mission_id"])
    plan(mission_id, "k-proposal-plan")
    stream = client.get("/v1/status/stream?limit=200", headers=auth("sim-scheduler-a")).text
    proposal_id = None
    for line in stream.splitlines():
        if not line.startswith("data: {"):
            continue
        event = json.loads(line[6:])
        if event["kind"] == "proposal.raised" and event["mission_id"] == mission_id:
            proposal_id = event["proposal_id"]
    assert proposal_id, "a rejection raises a replanning proposal for the operator"

    preview = client.post(
        f"/v1/missions/{mission_id}/proposals/{proposal_id}/preview",
        json={"reason_code": "operator_checks_consequence", "idempotency_key": "p-prev"},
        headers=auth("sim-scheduler-a", "p-prev"),
    )
    assert preview.status_code == 200
    accepted = client.post(
        f"/v1/missions/{mission_id}/proposals/{proposal_id}/accept",
        json={"reason_code": "accept_replan_next_aircraft", "idempotency_key": "p-acc"},
        headers=auth("sim-scheduler-a", "p-acc"),
    )
    assert accepted.status_code == 200
    body = accepted.json()
    assert body["status"] in {"planning", "eligible", "reserved_pending_commit", "accepted"}
    assert body["status"] not in {"activated", "in_progress"}, (
        "accepting a proposal tasks no aircraft"
    )
    assert body["authorized_for_flight"] is False


def test_cancel_records_the_workflow_state_with_a_reason(
    client: TestClient,
    auth: Callable[..., dict[str, str]],
    create_order: Callable[..., dict[str, Any]],
) -> None:
    order = create_order("k-cancel")
    response = client.post(
        f"/v1/orders/{order['order_id']}/cancel",
        json={
            "reason_code": "partner_withdraws_order",
            "note": "no longer required",
            "idempotency_key": "c-1",
        },
        headers=auth("sim-partner-a", "c-1"),
    )
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    audit = client.get("/v1/audit-events", headers=auth("sim-auditor-a")).json()["items"]
    kinds = {event["operation_kind"]: event for event in audit}
    assert "mission_cancelled" in kinds
    assert kinds["mission_cancelled"]["reason_code"] == "partner_withdraws_order"


def test_fleet_and_facility_reads_are_tenant_scoped_and_simulated(
    client: TestClient,
    auth: Callable[..., dict[str, str]],
    create_order: Callable[..., dict[str, Any]],
    committed: dict[str, Any],
) -> None:
    order = create_order("k-scope")
    assert order["tenant_id"]
    aircraft = client.get("/v1/aircraft", headers=auth("sim-scheduler-a")).json()
    assert aircraft["source_kind"] == "synthetic"
    assert {item["tenant_id"] for item in aircraft["items"]} == {
        "22222222-2222-2222-2222-222222222222"
    }
    facilities = client.get("/v1/facilities", headers=auth("sim-scheduler-a")).json()["items"]
    facility_id = next(item["facility_id"] for item in facilities if item["name"].startswith("hub"))
    capacity = client.get(
        f"/v1/facilities/{facility_id}/capacity", headers=auth("sim-scheduler-a")
    ).json()
    assert capacity["pad_capacity"] >= 1
    assert capacity["pads_committed"] >= 1
    assert capacity["simulation_notice"] == NOTICE

    outage = client.post(
        f"/v1/facilities/{facility_id}/outage",
        json={
            "reason_code": "pad_closed_for_survey",
            "valid_from": "2026-10-01T08:00:00Z",
            "valid_until": "2026-10-01T11:00:00Z",
            "idempotency_key": "o-1",
        },
        headers=auth("sim-scheduler-a", "o-1"),
    )
    assert outage.status_code == 200
    assert outage.json()["status"] == "outage"
    assert outage.json()["outage_reason"] == "pad_closed_for_survey"


def test_reservations_and_audit_are_listable_and_simulation_only(
    client: TestClient, auth: Callable[..., dict[str, str]], committed: dict[str, Any]
) -> None:
    window = client.get(
        "/v1/reservations?time_from=2026-09-30T00:00:00Z&time_to=2026-10-02T00:00:00Z",
        headers=auth("sim-scheduler-a"),
    ).json()
    assert window["items"] and all(item["state"] == "committed" for item in window["items"])  # noqa: PT018
    audit = client.get("/v1/audit-events", headers=auth("sim-auditor-a")).json()["items"]
    assert audit
    for event in audit:
        assert event["actor_id"] and event["config_release_sha"].startswith("sha256:")  # noqa: PT018
        assert event["source_kind"] == "committed"
        assert event["simulation_notice"] == NOTICE
    hashes = [event["record_hash"] for event in audit]
    assert len(set(hashes)) == len(hashes), "the audit chain is append-only"
    for previous, current in pairwise(audit):
        assert current["prev_hash"] == previous["record_hash"]


def test_simulation_run_and_replay_serve_the_frozen_snapshot(
    client: TestClient, auth: Callable[..., dict[str, str]], config_release: str
) -> None:
    run_id = "run-chandigarh-synthetic-v1-policy-a"
    run = client.get(f"/v1/simulations/{run_id}", headers=auth("sim-scheduler-a")).json()
    assert run["source_kind"] == "synthetic"
    assert run["authoritative"] is False
    assert run["run_identity"]["config_release_sha256"] == config_release

    before = client.get(
        f"/v1/simulations/{run_id}/replay-diff", headers=auth("sim-scheduler-a")
    ).json()
    assert before["event_hash_equal"] is True
    assert before["metrics_equal"] is True
    assert before["replayed_against"].startswith("corridor-graph bundle")

    # move the live world: the replay must not follow it
    facilities = client.get("/v1/facilities", headers=auth("sim-scheduler-a")).json()["items"]
    hub = next(item["facility_id"] for item in facilities if item["name"].startswith("hub"))
    client.post(
        f"/v1/facilities/{hub}/outage",
        json={
            "reason_code": "crane_on_pad",
            "valid_from": "2026-10-03T08:00:00Z",
            "valid_until": "2026-10-03T11:00:00Z",
            "idempotency_key": "o-replay",
        },
        headers=auth("sim-scheduler-a", "o-replay"),
    )
    after = client.get(
        f"/v1/simulations/{run_id}/replay-diff", headers=auth("sim-scheduler-a")
    ).json()
    assert after["run_a"] == before["run_a"], "replay serves the frozen bundle, not current state"
    assert after["current_world_digest"] == before["current_world_digest"], (
        "the replay plane is the corridor graph itself: an outage in the live world cannot "
        "move a re-run of a frozen seed"
    )

    comparison = client.get(
        "/v1/scenarios/run-chandigarh-synthetic-v1-policy-a/compare?against"
        "=run-chandigarh-synthetic-v1-policy-b",
        headers=auth("sim-scheduler-a"),
    ).json()
    assert "completes more" in comparison["verdict"] or "tied" in comparison["verdict"]
    assert comparison["compared_metrics"]["on_time_rate"]
    assert comparison["repetitions"] >= 10


def test_the_committed_trace_still_explains_which_candidates_lost(
    client: TestClient, auth: Callable[..., dict[str, str]], committed: dict[str, Any]
) -> None:
    """A commitment does not delete its own explanation.

    The operator reading a committed mission must still see the candidate ranking and the
    reasons the other candidates were refused - that is the audit trail Phase 4 promises.
    """

    trace = client.get(
        f"/v1/missions/{committed['mission_id']}/decision", headers=auth("sim-scheduler-a")
    ).json()
    assert trace["record"]["committed"] is True
    assert trace["record"]["outcome"] == "committed"
    assert trace["primary_reason_code"] == "ledger.committed"
    assert trace["ranking"], "the plan-stage ranking survives the commit"
    assert trace["rejected"][0]["human_reason"]
    assert trace["rejected"][0]["technical_detail"]
    assert trace["lock_order"] == [item["resource_key"] for item in committed["reservations"]]
    assert trace["source_kind"] == "committed"
    assert trace["authoritative"] is True
    assert trace["simulation_notice"] == NOTICE
