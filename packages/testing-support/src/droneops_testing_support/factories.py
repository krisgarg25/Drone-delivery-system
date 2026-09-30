"""Deterministic contract factories.

Every factory builds a valid contract instance with sensible defaults and accepts
field overrides. Randomness is only introduced through an explicit ``seed`` so no
value is produced without a recorded seed (conventions section 10).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from droneops_contracts import (
    Aircraft,
    AircraftModel,
    AircraftState,
    AltitudeReference,
    AuditLogEntry,
    ChargerBay,
    CoordinateTimeProfile,
    CrewAssignment,
    DecisionRecord,
    EligibleCandidate,
    EnergyEstimate,
    EventEnvelope,
    EventStreamType,
    EventType,
    Facility,
    HandoverType,
    Integrity,
    Kinematics,
    Link,
    LinkQuality,
    MaintenanceEvent,
    Mission,
    OperationalState,
    Order,
    OrderStatus,
    Outbox,
    Pad,
    PlanPayload,
    PlanVersion,
    Position,
    Power,
    PriorityClass,
    ProtectedVolumePolicy,
    Provenance,
    Reservation,
    ResourceCatalog,
    RoutePoint,
    RunIdentity,
    ScenarioBundle,
    SimulatedOrderRequest,
    TelemetryObservation,
    TimeWindow,
    UncertaintyState,
    UsageCharge,
)

UTC = UTC
BASE_DT = datetime(2026, 10, 1, 9, 0, 0, tzinfo=UTC)
CFG_SHA = "sha256:" + "a" * 64
UUID_TENANT = uuid.UUID(int=2)
UUID_MISSION = uuid.UUID(int=3)
UUID_AIRCRAFT = uuid.UUID(int=7)


def hex64(value: int) -> str:
    """A 64-hex digest derived from a small integer, for deterministic fixtures."""

    return format(value, "064x")


def utc_offset(seconds: int) -> datetime:
    return BASE_DT + timedelta(seconds=seconds)


def default_provenance(**overrides: object) -> Provenance:
    data = {
        "source": "simulator",
        "retrieved_at": BASE_DT,
        "expires_at": utc_offset(3600),
        "freshness": "fresh",
    }
    data.update(overrides)
    return Provenance(**data)  # type: ignore[arg-type]


def make_position(**overrides: object) -> Position:
    data = {
        "lat": 30.7305,
        "lon": 76.7812,
        "altitude_m": 60.0,
        "altitude_reference": AltitudeReference.AGL,
        "uncertainty_m": 5.0,
    }
    data.update(overrides)
    return Position(**data)  # type: ignore[arg-type]


def make_time_window(**overrides: object) -> TimeWindow:
    data = {"start_utc": BASE_DT, "end_utc": utc_offset(600), "version": 1}
    data.update(overrides)
    return TimeWindow(**data)  # type: ignore[arg-type]


def make_order(**overrides: object) -> Order:
    data = {
        "id": uuid.UUID(int=1),
        "tenant_id": UUID_TENANT,
        "external_ref_hash": hex64(1),
        "origin_facility_id": "hub-north",
        "destination_facility_id": "node-hospital-a",
        "payload_profile_id": "diagnostic-kit-v1",
        "priority_class": PriorityClass.ROUTINE,
        "earliest_departure": BASE_DT,
        "requested_deadline": utc_offset(1200),
        "status": OrderStatus.RECEIVED,
        "idempotency_key": "client-generated-1",
    }
    data.update(overrides)
    return Order(**data)  # type: ignore[arg-type]


def make_simulated_order_request(**overrides: object) -> SimulatedOrderRequest:
    data = {
        "origin_facility_id": "hub-north",
        "destination_facility_id": "node-hospital-a",
        "priority_class": PriorityClass.ROUTINE,
        "altitude_reference": AltitudeReference.AGL,
        "idempotency_key": "client-generated-1",
    }
    data.update(overrides)
    return SimulatedOrderRequest(**data)  # type: ignore[arg-type]


def make_mission(**overrides: object) -> Mission:
    data = {
        "id": UUID_MISSION,
        "order_id": uuid.UUID(int=1),
        "tenant_id": UUID_TENANT,
        "aircraft_id": UUID_AIRCRAFT,
        "status": "draft",
        "priority_class": PriorityClass.ROUTINE,
        "policy_version": "sim-policy-v1",
    }
    data.update(overrides)
    return Mission(**data)  # type: ignore[arg-type]


def make_protected_volume_policy(**overrides: object) -> ProtectedVolumePolicy:
    data = {
        "schema_version": 1,
        "policy_version": "sim-policy-v1",
        "altitude_reference": AltitudeReference.AGL,
        "horizontal_margin_m": 15,
        "vertical_margin_m": 10,
        "position_uncertainty_m": 5,
        "timing_uncertainty_s": 5,
        "transition_buffer_s": 10,
    }
    data.update(overrides)
    return ProtectedVolumePolicy(**data)  # type: ignore[arg-type]


def make_energy_estimate(**overrides: object) -> EnergyEstimate:
    data = {
        "profile_id": "airframe-x-sim",
        "estimated_consumption_wh": 85,
        "reserve_required_wh": 119,
        "margin_wh": 34,
    }
    data.update(overrides)
    return EnergyEstimate(**data)  # type: ignore[arg-type]


def make_route_point(**overrides: object) -> RoutePoint:
    data = {"t": 0, "position": make_position(), "speed_ms": 10.0}
    data.update(overrides)
    return RoutePoint(**data)  # type: ignore[arg-type]


def make_coordinate_time_profile(**overrides: object) -> CoordinateTimeProfile:
    data = {
        "crs": "EPSG:4326",
        "points": [
            make_route_point(t=0),
            make_route_point(t=60, position=make_position(altitude_m=60.0)),
        ],
    }
    data.update(overrides)
    return CoordinateTimeProfile(**data)  # type: ignore[arg-type]


def make_plan_payload(**overrides: object) -> PlanPayload:
    data = {
        "plan_version": "1.0",
        "route": make_coordinate_time_profile(),
        "protected_volume": make_protected_volume_policy(),
        "resources": ["corridor:hub-north:node-a:segment-7", "pad:node-a:pad-1"],
        "energy": make_energy_estimate(),
        "config_release_sha": CFG_SHA,
    }
    data.update(overrides)
    return PlanPayload(**data)  # type: ignore[arg-type]


def make_plan_version(**overrides: object) -> PlanVersion:
    data = {
        "id": uuid.UUID(int=4),
        "tenant_id": UUID_TENANT,
        "mission_id": UUID_MISSION,
        "route_hash": hex64(4),
        "energy_margin_wh": 34,
        "config_release_sha": CFG_SHA,
        "created_at": BASE_DT,
        "provenance": default_provenance(),
    }
    data.update(overrides)
    return PlanVersion(**data)  # type: ignore[arg-type]


def make_reservation(**overrides: object) -> Reservation:
    data = {
        "id": uuid.UUID(int=5),
        "tenant_id": UUID_TENANT,
        "mission_id": UUID_MISSION,
        "resource_key": "corridor:hub-north:node-a:segment-7",
        "resource_kind": "corridor",
        "valid_from": BASE_DT,
        "valid_until": utc_offset(600),
        "state": "committed",
        "commitment_token": "tok-1",
        "created_at": BASE_DT,
    }
    data.update(overrides)
    return Reservation(**data)  # type: ignore[arg-type]


def make_resource_catalog(**overrides: object) -> ResourceCatalog:
    data = {
        "resource_key": "corridor:hub-north:node-a:segment-7",
        "kind": "corridor",
        "owner_tenant_id": UUID_TENANT,
        "capacity": 1,
        "status": "active",
        "source_release": CFG_SHA,
    }
    data.update(overrides)
    return ResourceCatalog(**data)  # type: ignore[arg-type]


def make_aircraft_model(**overrides: object) -> AircraftModel:
    data = {
        "id": uuid.UUID(int=8),
        "name": "Airframe X (illustrative)",
        "vendor": "illustrative-vendor",
        "type_cert_status": "unverified-simulated",
        "empty_mass_g": 2400,
        "max_payload_g": 2000,
        "cruise_speed_ms": 12.0,
        "max_wind_ms": 8.0,
        "battery_profile_id": "airframe-x-sim",
        "created_at": BASE_DT,
    }
    data.update(overrides)
    return AircraftModel(**data)  # type: ignore[arg-type]


def make_aircraft(**overrides: object) -> Aircraft:
    data = {
        "id": UUID_AIRCRAFT,
        "tenant_id": UUID_TENANT,
        "model_id": uuid.UUID(int=8),
        "registration_ref": "TG-SIM-0004",
        "status": "available",
        "home_facility_id": uuid.UUID(int=10),
    }
    data.update(overrides)
    return Aircraft(**data)  # type: ignore[arg-type]


def make_aircraft_state(**overrides: object) -> AircraftState:
    data = {
        "aircraft_id": UUID_AIRCRAFT,
        "tenant_id": UUID_TENANT,
        "measurement_time": BASE_DT,
        "received_time": utc_offset(1),
        "operational_state": OperationalState.AVAILABLE,
        "battery_pct": 82.0,
        "energy_wh_remaining": 112,
        "position": make_position(),
        "uncertainty_m": 5.0,
        "link_quality": LinkQuality.GOOD,
        "emergency_state": "none",
    }
    data.update(overrides)
    return AircraftState(**data)  # type: ignore[arg-type]


def make_maintenance_event(**overrides: object) -> MaintenanceEvent:
    data = {
        "id": uuid.UUID(int=11),
        "tenant_id": UUID_TENANT,
        "aircraft_id": UUID_AIRCRAFT,
        "occurred_at": BASE_DT,
        "event_type": "scheduled_service",
        "severity": "low",
        "notes": "illustrative",
    }
    data.update(overrides)
    return MaintenanceEvent(**data)  # type: ignore[arg-type]


def make_facility(**overrides: object) -> Facility:
    data = {
        "id": uuid.UUID(int=10),
        "tenant_id": UUID_TENANT,
        "name": "hub-north-synthetic",
        "kind": "hub",
        "address_ref": "addr-1",
        "boundary_hash": hex64(10),
        "status": "active",
        "source": "osm-synthetic",
        "source_updated_at": BASE_DT,
        "provenance": default_provenance(),
    }
    data.update(overrides)
    return Facility(**data)  # type: ignore[arg-type]


def make_pad(**overrides: object) -> Pad:
    data = {
        "id": uuid.UUID(int=12),
        "tenant_id": UUID_TENANT,
        "facility_id": uuid.UUID(int=10),
        "name": "pad-1",
        "capacity": 1,
        "approach_profile_id": "approach-a",
        "handover_type": HandoverType.LAND_TRANSFER,
        "status": "active",
    }
    data.update(overrides)
    return Pad(**data)  # type: ignore[arg-type]


def make_charger_bay(**overrides: object) -> ChargerBay:
    data = {
        "id": uuid.UUID(int=13),
        "tenant_id": UUID_TENANT,
        "facility_id": uuid.UUID(int=10),
        "pad_id": uuid.UUID(int=12),
        "connector_type": "ccs-sim",
        "recharge_minutes": 40,
        "status": "active",
    }
    data.update(overrides)
    return ChargerBay(**data)  # type: ignore[arg-type]


def make_crew_assignment(**overrides: object) -> CrewAssignment:
    data = {
        "id": uuid.UUID(int=14),
        "tenant_id": UUID_TENANT,
        "facility_id": uuid.UUID(int=10),
        "crew_id": uuid.UUID(int=99),
        "duty_window": make_time_window(),
        "role": "scheduler_operator",
        "status": "on_duty",
    }
    data.update(overrides)
    return CrewAssignment(**data)  # type: ignore[arg-type]


def make_telemetry_observation(**overrides: object) -> TelemetryObservation:
    data = {
        "observation_id": uuid.UUID(int=15),
        "tenant_id": UUID_TENANT,
        "aircraft_id": UUID_AIRCRAFT,
        "message_type": "telemetry.position",
        "sequence": 412,
        "measurement_time": utc_offset(72),
        "received_time": utc_offset(73),
        "position": make_position(altitude_m=62.0),
        "kinematics": Kinematics(speed_ms=9.8, heading_deg=87.0),
        "power": Power(battery_pct=82.0, energy_wh_remaining=112),
        "link": Link(quality=LinkQuality.GOOD, rtt_ms=140),
        "integrity": Integrity(),
        "raw_hash": hex64(15),
        "source_adapter": "simulator",
        "provenance": default_provenance(),
    }
    data.update(overrides)
    return TelemetryObservation(**data)  # type: ignore[arg-type]


def make_uncertainty_state(**overrides: object) -> UncertaintyState:
    data = {
        "aircraft_id": UUID_AIRCRAFT,
        "tenant_id": UUID_TENANT,
        "state": "clear",
        "last_accepted_measurement_time": BASE_DT,
        "staleness_s": 0,
        "protected_volume_retained": True,
    }
    data.update(overrides)
    return UncertaintyState(**data)  # type: ignore[arg-type]


def make_decision_record(**overrides: object) -> DecisionRecord:
    data = {
        "id": uuid.UUID(int=16),
        "tenant_id": UUID_TENANT,
        "mission_id": UUID_MISSION,
        "input_hash": hex64(16),
        "eligible_candidates": [
            EligibleCandidate(
                plan_version_id=uuid.UUID(int=4),
                aircraft_id=UUID_AIRCRAFT,
                lateness_s=0,
                energy_margin_wh=34,
                disturbance_score=1.0,
                fairness_score=1.0,
            )
        ],
        "rejected_candidates": [],
        "selected_plan_id": uuid.UUID(int=4),
        "reason_codes": ["energy_margin_sufficient"],
        "deciding_actor": "system",
        "created_at": BASE_DT,
        "config_release_sha": CFG_SHA,
        "provenance": default_provenance(),
    }
    data.update(overrides)
    return DecisionRecord(**data)  # type: ignore[arg-type]


def make_event_envelope(**overrides: object) -> EventEnvelope:
    data = {
        "schema_version": 1,
        "event_id": uuid.UUID(int=17),
        "event_type": EventType.RESERVATION_COMMITTED,
        "occurred_at": BASE_DT,
        "tenant_id": UUID_TENANT,
        "stream_type": EventStreamType.MISSION,
        "stream_id": UUID_MISSION,
        "seq": 18,
        "correlation_id": "req-0001",
        "causation_id": None,
        "actor": "system",
        "config_release_sha": CFG_SHA,
        "payload": {},
    }
    data.update(overrides)
    return EventEnvelope(**data)  # type: ignore[arg-type]


def make_outbox(**overrides: object) -> Outbox:
    data = {
        "event_id": uuid.UUID(int=17),
        "tenant_id": UUID_TENANT,
        "published_at": None,
        "attempts": 0,
        "last_error": None,
    }
    data.update(overrides)
    return Outbox(**data)  # type: ignore[arg-type]


def make_audit_log_entry(**overrides: object) -> AuditLogEntry:
    data = {
        "id": uuid.UUID(int=18),
        "tenant_id": UUID_TENANT,
        "occurred_at": BASE_DT,
        "actor_type": "system",
        "actor_id": "dispatcher",
        "action": "reservation.commit",
        "resource_type": "mission",
        "resource_id": str(UUID_MISSION),
        "before_sha256": None,
        "after_sha256": hex64(18),
        "request_id": "req-0001",
        "ip_hash": None,
        "device_hash": None,
        "reason_code": "ok",
        "config_release_sha": CFG_SHA,
        "prev_hash": hex64(0),
        "record_hash": hex64(19),
    }
    data.update(overrides)
    return AuditLogEntry(**data)  # type: ignore[arg-type]


def make_usage_charge(**overrides: object) -> UsageCharge:
    data = {
        "id": uuid.UUID(int=20),
        "tenant_id": UUID_TENANT,
        "mission_id": UUID_MISSION,
        "charge_kind": "simulated_delivery",
        "amount_minor_units": 0,
        "currency": "INR",
        "idempotency_key": "charge-1",
        "charged_at": BASE_DT,
    }
    data.update(overrides)
    return UsageCharge(**data)  # type: ignore[arg-type]


def make_run_identity(**overrides: object) -> RunIdentity:
    data = {
        "bundle_sha256": hex64(30),
        "engine_version": "sim-1.0.0",
        "scenario_sha256": hex64(31),
        "policy_version": "sim-policy-v1",
        "config_release_sha256": CFG_SHA,
        "seed": 831,
        "python_version": "3.13",
        "machine_architecture": "x86_64",
    }
    data.update(overrides)
    return RunIdentity(**data)  # type: ignore[arg-type]


def make_scenario_bundle(**overrides: object) -> ScenarioBundle:
    data = {
        "schema_version": 1,
        "scenario_id": "chandigarh-synthetic-v1",
        "authoritative": False,
        "network": {"facilities": [], "corridors": []},
        "fleet": {"models": [], "aircraft": []},
        "demand": {"orders": []},
        "policy": make_protected_volume_policy(),
        "weather_seed": 93214,
        "faults": [],
        "source_manifest": {"osm_extract_date": "2026-09-01"},
    }
    data.update(overrides)
    return ScenarioBundle(**data)  # type: ignore[arg-type]
