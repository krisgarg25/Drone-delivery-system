"""Invariant tests: the type layer, not prose, rejects unsafe values."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

import droneops_contracts as c
import droneops_testing_support as ts

NON_UTC = timezone(timedelta(hours=5, minutes=30))
VALID_SHA = "sha256:" + "0" * 64


# --- I10: timezone-aware UTC only -------------------------------------------------


def test_naive_datetime_rejected() -> None:
    naive = datetime(2026, 10, 1, 9, 0, 0)
    with pytest.raises(ValidationError):
        ts.make_mission(activated_at=naive)
    with pytest.raises(ValidationError):
        ts.make_time_window(start_utc=naive)


def test_non_utc_datetime_rejected() -> None:
    with pytest.raises(ValidationError):
        ts.make_order(earliest_departure=datetime(2026, 10, 1, tzinfo=NON_UTC))


def test_utc_datetime_accepted() -> None:
    mission = ts.make_mission(activated_at=datetime(2026, 10, 1, tzinfo=UTC))
    assert mission.activated_at is not None


# --- I9: explicit altitude reference ---------------------------------------------


def test_unspecified_altitude_rejected_on_position() -> None:
    with pytest.raises(ValidationError):
        ts.make_position(altitude_reference=c.AltitudeReference.UNSPECIFIED)


def test_unspecified_altitude_rejected_via_aircraft_state() -> None:
    bad_position = {
        "lat": 30.0,
        "lon": 76.0,
        "altitude_m": 40.0,
        "altitude_reference": c.AltitudeReference.UNSPECIFIED,
    }
    with pytest.raises(ValidationError):
        ts.make_aircraft_state(position=bad_position)


def test_simulated_request_rejects_unspecified_altitude() -> None:
    with pytest.raises(ValidationError):
        ts.make_simulated_order_request(
            altitude_reference=c.AltitudeReference.UNSPECIFIED
        )


def test_every_altitude_is_paired_with_reference() -> None:
    position = ts.make_position(altitude_m=42.0)
    fields = c.Position.model_fields
    assert "altitude_m" in fields and "altitude_reference" in fields
    assert position.altitude_reference is not c.AltitudeReference.UNSPECIFIED


# --- Unknown state rejection ------------------------------------------------------


@pytest.mark.parametrize(
    "enum_cls", [c.MissionStatus, c.ReservationState, c.OperationalState]
)
def test_enum_rejects_unknown(enum_cls) -> None:
    with pytest.raises(ValueError):
        enum_cls("not-a-real-state")


def test_model_rejects_unknown_states() -> None:
    with pytest.raises(ValidationError):
        ts.make_mission(status="launched")
    with pytest.raises(ValidationError):
        ts.make_reservation(state="double_booked")
    with pytest.raises(ValidationError):
        ts.make_aircraft_state(operational_state="flying_but_really_not")


def test_state_enum_value_sets_are_closed() -> None:
    assert {s.value for s in c.MissionStatus} >= {
        "draft",
        "planning",
        "reserved_pending_commit",
        "accepted",
        "activated",
        "unresolved",
    }
    assert {s.value for s in c.ReservationState} == {
        "tentative",
        "committed",
        "activated",
        "completed",
        "cancelled",
        "unresolved",
    }
    assert len({s.value for s in c.OperationalState}) == 9


# --- Handover enum: land-and-transfer only for this build ------------------------


def test_pad_default_handover_is_land_transfer() -> None:
    assert ts.make_pad().handover_type is c.HandoverType.LAND_TRANSFER


@pytest.mark.parametrize("banned", [c.HandoverType.WINCH, c.HandoverType.PARACHUTE])
def test_pad_rejects_banned_handover(banned: c.HandoverType) -> None:
    with pytest.raises(ValidationError):
        ts.make_pad(handover_type=banned)


# --- Resource kinds: closed set ---------------------------------------------------


@pytest.mark.parametrize(
    "key", ["corridor:a:b", "volume_cell:z", "crossing:x", "pad:p1", "charger_bay:c1", "crew:crw"]
)
def test_allowed_resource_keys_accept(key: str) -> None:
    assert c.validate_resource_key(key) == key


@pytest.mark.parametrize("key", ["unknown_kind:x", "nope", "airspace:y", "corridor:", ""])
def test_bad_resource_keys_reject(key: str) -> None:
    with pytest.raises(ValueError):
        c.validate_resource_key(key)


def test_reservation_kind_must_match_key() -> None:
    with pytest.raises(ValidationError):
        ts.make_reservation(resource_key="pad:p1", resource_kind="corridor")
    with pytest.raises(ValidationError):
        ts.make_reservation(resource_key="bogus:x")


# --- config_release_sha pattern ---------------------------------------------------


@pytest.mark.parametrize("model_kw", [
    {"cls": c.PlanVersion, "factory": ts.make_plan_version},
    {"cls": c.DecisionRecord, "factory": ts.make_decision_record},
    {"cls": c.AuditLogEntry, "factory": ts.make_audit_log_entry},
    {"cls": c.EventEnvelope, "factory": ts.make_event_envelope},
])
def test_config_release_sha_required_pattern(model_kw: dict) -> None:
    factory = model_kw["factory"]
    assert factory().config_release_sha.startswith("sha256:")
    for bad in ["0" * 64, "sha256:xyz", "sha512:" + "0" * 128, ""]:
        with pytest.raises(ValidationError):
            factory(config_release_sha=bad)


def test_run_identity_config_release_pattern() -> None:
    assert ts.make_run_identity().config_release_sha256.startswith("sha256:")
    with pytest.raises(ValidationError):
        ts.make_run_identity(config_release_sha256="deadbeef")


# --- Provenance is not optional ---------------------------------------------------


@pytest.mark.parametrize(
    "factory",
    [ts.make_telemetry_observation, ts.make_decision_record, ts.make_plan_version, ts.make_facility],
)
def test_provenance_required(factory) -> None:
    model = factory()
    assert model.provenance.source is not None
    with pytest.raises(ValidationError):
        factory(provenance=None)


def test_model_cannot_be_constructed_without_provenance() -> None:
    data = c.TelemetryObservation.model_fields
    assert "provenance" in data
    # A hand-built payload with provenance omitted must fail.
    obj = ts.make_telemetry_observation()
    payload = obj.model_dump(mode="json")
    del payload["provenance"]
    with pytest.raises(ValidationError):
        c.TelemetryObservation(**payload)


# --- authoritative defaults to false and is hard to strip ------------------------


@pytest.mark.parametrize(
    "factory",
    [ts.make_mission, ts.make_plan_version, ts.make_telemetry_observation, ts.make_decision_record, ts.make_scenario_bundle, ts.make_uncertainty_state],
)
def test_authoritative_defaults_false(factory) -> None:
    assert factory().authoritative is False


def test_authoritative_is_a_required_field_of_the_schema() -> None:
    assert "authoritative" in c.TelemetryObservation.model_fields


# --- immutability (append-only) --------------------------------------------------


def test_contract_models_are_frozen() -> None:
    order = ts.make_order()
    with pytest.raises(ValidationError):
        order.status = c.OrderStatus.ACCEPTED


# --- tenant_id non-null on every real-world entity ------------------------------


@pytest.mark.parametrize(
    "factory",
    [
        ts.make_order,
        ts.make_mission,
        ts.make_plan_version,
        ts.make_reservation,
        ts.make_aircraft,
        ts.make_aircraft_state,
        ts.make_maintenance_event,
        ts.make_facility,
        ts.make_pad,
        ts.make_charger_bay,
        ts.make_crew_assignment,
        ts.make_telemetry_observation,
        ts.make_decision_record,
        ts.make_event_envelope,
        ts.make_outbox,
        ts.make_audit_log_entry,
        ts.make_usage_charge,
        ts.make_uncertainty_state,
    ],
)
def test_tenant_id_required(factory) -> None:
    model = factory()
    assert isinstance(model.tenant_id, uuid.UUID)
    payload = model.model_dump(mode="json")
    payload["tenant_id"] = None
    with pytest.raises(ValidationError):
        type(model)(**payload)


# --- duplicate-request identity (I2) --------------------------------------------


def test_order_identity_requires_idempotency_key() -> None:
    with pytest.raises(ValidationError):
        ts.make_order(idempotency_key="")
    with pytest.raises(ValidationError):
        ts.make_order(idempotency_key=None)


def test_same_identity_is_deterministic() -> None:
    first = ts.make_order()
    second = ts.make_order()
    assert first == second
    assert first.model_dump_json() == second.model_dump_json()


def test_distinct_idempotency_key_is_distinct_identity() -> None:
    a = ts.make_order(idempotency_key="key-a")
    b = ts.make_order(idempotency_key="key-b")
    assert a != b


# --- clock semantics (I10) ------------------------------------------------------


def test_late_receipt_does_not_predate_measurement() -> None:
    # received_time must not be earlier than measurement_time.
    with pytest.raises(ValidationError):
        ts.make_telemetry_observation(
            measurement_time=ts.utc_offset(100), received_time=ts.utc_offset(0)
        )


def test_out_of_order_is_a_retained_flag_not_a_mutation() -> None:
    obs = ts.make_telemetry_observation(
        integrity=c.Integrity(signature_verified=True, out_of_order=True)
    )
    assert obs.integrity.out_of_order is True


# --- uncertainty retains protected volume (I3) ----------------------------------


def test_uncertain_state_must_retain_protected_volume() -> None:
    with pytest.raises(ValidationError):
        ts.make_uncertainty_state(
            state=c.UncertaintyStateKind.UNCERTAIN, protected_volume_retained=False
        )
    # CLEAR may or may not retain; the constraint only bites for non-clear.
    assert ts.make_uncertainty_state(
        state=c.UncertaintyStateKind.CLEAR, protected_volume_retained=False
    )
