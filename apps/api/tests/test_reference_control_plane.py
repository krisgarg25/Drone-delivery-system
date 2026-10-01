"""Unit tests for the reference control plane behind the ports.

These cover the rules a router cannot see: identity is content-addressed so a replayed key
lands on the same mission; claims are locked in canonical order; a cancellation that races
a commitment resolves deterministically; an outage restricts eligibility rather than
touching an aircraft; and no port in the system accepts an aircraft write.
"""

from __future__ import annotations

import inspect
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from droneops_api.ports import (
    ALL_CAPABILITIES,
    CancelDraft,
    CommitDraft,
    DuplicateCommitError,
    FacilityPort,
    FleetPort,
    MissionPort,
    OrderDraft,
    OrderPort,
    OutageDraft,
    PlanDraft,
    Principal,
    ReservationPort,
    SimulationPort,
)
from droneops_api.reference import CONFIG_RELEASE_SHA, InMemoryControlPlane
from droneops_api.security import ROLE_CAPABILITIES

TENANT = uuid.UUID("22222222-2222-2222-2222-222222222222")
BASE = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


def scheduler() -> Any:
    return Principal(
        tenant_id=TENANT,
        actor_id="actor-scheduler",
        actor_type="user",
        roles=("scheduler_operator",),
        capabilities=ROLE_CAPABILITIES["scheduler_operator"],
        request_id="req-unit",
        config_release_sha=CONFIG_RELEASE_SHA,
    )


def draft(key: str = "unit-1", **overrides: Any) -> OrderDraft:
    values: dict[str, Any] = {
        "external_ref": "unit",
        "origin_facility_id": "hub-north",
        "destination_facility_id": "node-hospital-a",
        "payload_profile_id": "diagnostic-kit-v1",
        "priority_class": "routine",
        "earliest_departure": BASE,
        "requested_deadline": BASE + timedelta(minutes=20),
        "idempotency_key": key,
    }
    values.update(overrides)
    return OrderDraft(**values)


@pytest.fixture(name="plane")
def plane_fixture() -> InMemoryControlPlane:
    return InMemoryControlPlane()


def test_ids_are_content_addressed_from_the_client_key(plane: InMemoryControlPlane) -> None:
    principal = scheduler()
    first = plane.create_order(principal, draft("stable-key"))
    again = plane.create_order(principal, draft("stable-key"))
    assert first.mission_id == again.mission_id
    assert first.order_id == again.order_id
    other = plane.create_order(principal, draft("other-key"))
    assert other.mission_id != first.mission_id


def test_plan_is_proposal_and_reserve_is_the_commitment(plane: InMemoryControlPlane) -> None:
    principal = scheduler()
    order = plane.create_order(principal, draft())
    plans = plane.plan(principal, order.mission_id, PlanDraft("p", CONFIG_RELEASE_SHA))
    assert plans.items and all(not item.authoritative for item in plans.items)  # noqa: PT018
    assert all(str(item.source_kind) in {"SourceKind.PLANNED", "planned"} for item in plans.items)

    reservations = plane.reserve(
        principal,
        order.mission_id,
        CommitDraft("r", plans.items[0].plan_version_id, CONFIG_RELEASE_SHA),
    )
    assert all(item.authoritative for item in reservations.items)
    assert all(item.state == "committed" for item in reservations.items)
    assert plane.missions[order.mission_id].mission.status.value == "accepted"


def test_claims_are_locked_in_canonical_order(plane: InMemoryControlPlane) -> None:
    principal = scheduler()
    order = plane.create_order(principal, draft())
    plans = plane.plan(principal, order.mission_id, PlanDraft("p", CONFIG_RELEASE_SHA))
    reservations = plane.reserve(
        principal,
        order.mission_id,
        CommitDraft("r", plans.items[0].plan_version_id, CONFIG_RELEASE_SHA),
    )
    trace = plane.decision(principal, order.mission_id)
    keys = [item.resource_key for item in reservations.items]
    assert len(keys) == len(set(keys))
    assert trace.lock_order == keys, "the ledger records the order it acquired resources in"
    assert {item.resource_kind for item in reservations.items} == {
        key.split(":", 1)[0] for key in keys
    }


def test_replan_is_rejected_against_the_wrong_configuration_release(
    plane: InMemoryControlPlane,
) -> None:
    principal = scheduler()
    order = plane.create_order(principal, draft())
    wrong = "sha256:" + "a" * 64
    plans = plane.plan(principal, order.mission_id, PlanDraft("p", wrong))
    assert plans.items == []
    trace = plane.decision(principal, order.mission_id)
    assert trace.rejected and trace.rejected[0].reason_code.startswith("config.")  # noqa: PT018
    assert wrong in trace.rejected[0].technical_detail


def test_cancellation_that_races_a_commitment_resolves_to_the_commitment(
    plane: InMemoryControlPlane,
) -> None:
    principal = scheduler()
    order = plane.create_order(principal, draft())
    plans = plane.plan(principal, order.mission_id, PlanDraft("p", CONFIG_RELEASE_SHA))
    plane.reserve(
        principal,
        order.mission_id,
        CommitDraft("r", plans.items[0].plan_version_id, CONFIG_RELEASE_SHA),
    )
    with pytest.raises(DuplicateCommitError):
        plane.cancel_mission(principal, order.mission_id, CancelDraft("c", "operator_stops"))
    assert plane.missions[order.mission_id].mission.status.value == "accepted"


def test_uncommitted_mission_cancels_and_records_the_reason(plane: InMemoryControlPlane) -> None:
    principal = scheduler()
    order = plane.create_order(principal, draft())
    mission = plane.cancel_mission(
        principal, order.mission_id, CancelDraft("c", "partner_withdrew")
    )
    assert mission.status == "cancelled"
    assert mission.authorized_for_flight is False
    audit = plane.audit[-1]
    assert audit.operation_kind == "mission_cancelled"
    assert audit.reason_code == "partner_withdrew"
    assert audit.actor_id == principal.actor_id


def test_outage_restricts_eligibility_without_touching_an_aircraft(
    plane: InMemoryControlPlane,
) -> None:
    principal = scheduler()
    before = {key: item.status for key, item in plane.aircraft.items()}
    facility = next(item for item in plane.facilities.values() if item.tenant_id == TENANT)
    plane.report_outage(
        principal,
        facility.facility_id,
        OutageDraft(
            "o", "pad_closed_for_survey", BASE - timedelta(hours=1), BASE + timedelta(hours=2)
        ),
    )
    order = plane.create_order(principal, draft())
    plans = plane.plan(principal, order.mission_id, PlanDraft("p", CONFIG_RELEASE_SHA))
    trace = plane.decision(principal, order.mission_id)
    assert trace.rejected
    assert any(item.reason_code == "ledger.candidate_restricted" for item in trace.rejected)
    assert "pad:hub-north" in trace.inputs.restricted_keys or facility.name in str(
        trace.inputs.restricted_keys
    )
    assert plans.items == [], "no route may occupy an outaged pad"
    assert {key: item.status for key, item in plane.aircraft.items()} == before, (
        "an outage restricts eligibility; it never touches an aircraft"
    )


def test_ports_expose_no_aircraft_mutation() -> None:
    mutating_verbs = (
        "command",
        "launch",
        "takeoff",
        "land",
        "rtl",
        "return",
        "upload",
        "parameter",
        "firmware",
        "arm",
        "start",
        "stop",
        "abort",
    )
    for port in (OrderPort, MissionPort, ReservationPort, FleetPort, FacilityPort, SimulationPort):
        for name, _ in inspect.getmembers(port, predicate=callable):
            if name.startswith("_"):
                continue
            lowered = name.lower()
            assert not any(verb in lowered for verb in mutating_verbs), f"{port.__name__}.{name}"
    assert not any("command" in capability for capability in ALL_CAPABILITIES)
    assert FleetPort.list is not None
    assert not hasattr(FleetPort, "write") and not hasattr(FleetPort, "task")  # noqa: PT018
    assert not hasattr(MissionPort, "launch")


def test_audit_chain_is_append_only(plane: InMemoryControlPlane) -> None:
    principal = scheduler()
    for index in range(3):
        plane.create_order(principal, draft(f"chain-{index}"))
    hashes = [event.record_hash for event in plane.audit]
    assert len(set(hashes)) == len(hashes)
    for previous, current in zip(plane.audit, plane.audit[1:], strict=False):
        assert current.prev_hash == previous.record_hash
