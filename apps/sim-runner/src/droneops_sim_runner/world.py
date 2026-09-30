"""Bundle documents -> pure `WorldSpec`, with schema and synthetic-honesty checks.

The runner is the only place YAML is parsed; sim-core never sees a file. Anything that
could let an unlabelled or authority-claiming object into a deterministic run fails
closed here: missing altitude references, missing `authoritative: false`, non-fictional
identifiers and geometry without provenance are load errors, not warnings.
"""

from __future__ import annotations

from typing import Any

from droneops_sim_core.model import (
    AircraftConfig,
    AircraftProfile,
    BatteryCurvePoint,
    Closure,
    CorridorEdge,
    Facility,
    Fault,
    GeometryPoint,
    Order,
    WeatherTracePoint,
    WorldSpec,
)

MICROSECONDS = 1_000_000
MINUTE_US = 60 * MICROSECONDS
FICTIONAL_SUFFIX = "-synthetic"


def _check_synthetic(doc: dict[str, Any], name: str) -> None:
    if doc.get("schema_version") != 1:
        raise ValueError(f"{name}: only schema_version 1 bundles are runnable")
    if doc.get("authoritative") is not False:
        raise ValueError(f"{name}: every world document must declare authoritative: false")


def _fictional(object_id: str, kind: str) -> str:
    # Never a real hospital, business or customer: fictional identifiers are enforced,
    # not requested (docs/conventions.md section 3).
    if not object_id.endswith(FICTIONAL_SUFFIX):
        raise ValueError(
            f"{kind} '{object_id}': Phase 1 objects must carry the '{FICTIONAL_SUFFIX}' suffix"
        )
    return object_id


def _point(raw: dict[str, Any], owner: str) -> GeometryPoint:
    reference = str(raw.get("altitude_reference", ""))
    if reference not in {"AGL", "AMSL", "GROUND", "PAD", "UNSPECIFIED"}:
        raise ValueError(f"{owner}: geometry point is missing an explicit altitude_reference")
    if reference != "AGL":
        raise ValueError(
            f"{owner}: altitude_reference must be AGL for Phase 1 geometry, got {reference}"
        )
    return GeometryPoint(
        lat_e6=int(raw["lat_e6"]),
        lon_e6=int(raw["lon_e6"]),
        alt_m_agl=int(raw["alt_m_agl"]),
        altitude_reference=reference,
    )


def build_profiles(fleet: dict[str, Any]) -> dict[str, AircraftProfile]:
    _check_synthetic(fleet, "fleet.yaml")
    profiles: dict[str, AircraftProfile] = {}
    for row in fleet.get("profiles", []):
        profile_id = str(row["profile_id"])
        curve = tuple(
            BatteryCurvePoint(payload_g=int(p["payload_g"]), wh_per_100km=int(p["wh_per_100km"]))
            for p in sorted(row["battery_curve"], key=lambda point: int(point["payload_g"]))
        )
        if not curve or curve[0].payload_g < 0:
            raise ValueError(f"{profile_id}: an explicit illustrative battery curve is required")
        profiles[profile_id] = AircraftProfile(
            profile_id=profile_id,
            max_payload_g=int(row["max_payload_g"]),
            cruise_speed_mps=int(row["cruise_speed_mps"]),
            capacity_wh=int(row["capacity_wh"]),
            reserve_fraction_bp=int(row["reserve_fraction_bp"]),
            charge_power_w=int(row["charge_power_w"]),
            battery_curve=curve,
            source=str(row.get("source", "synthetic")),
            illustrative=bool(row.get("illustrative", True)),
        )
    if len(profiles) < 2:
        raise ValueError("Phase 1 requires at least two aircraft profiles")
    return profiles


def build_aircraft(
    fleet: dict[str, Any], profiles: dict[str, AircraftProfile]
) -> tuple[AircraftConfig, ...]:
    rows: list[AircraftConfig] = []
    for row in fleet.get("aircraft", []):
        aircraft_id = _fictional(str(row["aircraft_id"]), "aircraft")
        if row["profile_id"] not in profiles:
            raise ValueError(f"{aircraft_id}: unknown profile {row['profile_id']}")
        rows.append(
            AircraftConfig(
                aircraft_id=aircraft_id,
                profile_id=str(row["profile_id"]),
                home_facility_id=_fictional(str(row["home_facility_id"]), "home facility"),
                initial_status=str(row.get("status", "available")),
                charge_pct=int(row.get("charge_pct", 100)),
                available_at_minute=int(row.get("available_at_minute", 0)),
                reposition_enabled=bool(row.get("reposition_enabled", True)),
            )
        )
    if not rows:
        raise ValueError("fleet.yaml declares no aircraft")
    return tuple(rows)


def build_facilities(doc: dict[str, Any]) -> dict[str, Facility]:
    _check_synthetic(doc, "facilities.yaml")
    facilities: dict[str, Facility] = {}
    for row in doc.get("facilities", []):
        facility_id = _fictional(str(row["facility_id"]), "facility")
        if row["kind"] not in {"hub", "node"}:
            raise ValueError(f"{facility_id}: kind must be hub or node")
        outage = row.get("outage") or {}
        facilities[facility_id] = Facility(
            facility_id=facility_id,
            kind=str(row["kind"]),
            name=str(row.get("name", f"{facility_id} (fictional)")),
            location=_point(dict(row["location"]), facility_id),
            pad_count=int(row["pads"]),
            charger_bays=int(row.get("charger_bays", 0)),
            charge_power_w=int(row.get("charge_power_w", 220)),
            loading_turnaround_us=int(row.get("loading_turnaround_min", 2)) * MINUTE_US,
            offload_turnaround_us=int(row.get("offload_turnaround_min", 1)) * MINUTE_US,
            open_minute=int(row.get("open_minute", 0)),
            close_minute=int(row.get("close_minute", 1440)),
            crew_shifts=tuple(
                (int(a), int(b)) for a, b in sorted(row.get("crew_shifts", [(0, 1440)]))
            ),
            outage_from_us=int(outage.get("from_minute", -1)) * MINUTE_US,
            outage_to_us=int(outage.get("to_minute", -1)) * MINUTE_US,
        )
    hubs = sorted(row for row in facilities if facilities[row].kind == "hub")
    nodes = sorted(row for row in facilities if facilities[row].kind == "node")
    if len(hubs) < 2 or len(nodes) < 2:
        raise ValueError(
            "topology must declare at least two hubs and two nodes; it is data-driven by design"
        )
    return facilities


def build_corridors(
    doc: dict[str, Any], facilities: dict[str, Facility]
) -> tuple[dict[str, CorridorEdge], tuple[Closure, ...], frozenset[str]]:
    _check_synthetic(doc, "corridors.yaml")
    if doc.get("altitude_reference") != "AGL":
        raise ValueError("corridors.yaml must declare altitude_reference: AGL for the whole graph")
    bands: dict[str, tuple[int, int]] = {}
    for band in doc.get("bands", []):
        bands[str(band["band"])] = (int(band["floor_m_agl"]), int(band["ceiling_m_agl"]))
    edges: dict[str, CorridorEdge] = {}
    in_degree: dict[str, int] = {}
    out_degree: dict[str, int] = {}
    for row in sorted(doc.get("edges", []), key=lambda item: str(item["corridor_id"])):
        corridor_id = _fictional(str(row["corridor_id"]), "corridor")
        for end in ("from", "to"):
            if row[end] not in facilities:
                raise ValueError(f"{corridor_id}: unknown endpoint {row[end]}")
        geometry = tuple(_point(p, corridor_id) for p in row.get("geometry", []))
        if len(geometry) < 2:
            raise ValueError(
                f"{corridor_id}: every corridor needs reference geometry with altitude metadata"
            )
        band = str(row["band"])
        if band not in bands:
            raise ValueError(f"{corridor_id}: band {band} is not declared")
        edges[corridor_id] = CorridorEdge(
            corridor_id=corridor_id,
            from_id=str(row["from"]),
            to_id=str(row["to"]),
            length_m=int(row["length_m"]),
            width_m=int(row["width_m"]),
            band=band,
            floor_m_agl=bands[band][0],
            ceiling_m_agl=bands[band][1],
            max_wind_ms=int(row.get("max_wind_ms", 12)),
            geometry=geometry,
        )
        in_degree[str(row["to"])] = in_degree.get(str(row["to"]), 0) + 1
        out_degree[str(row["from"])] = out_degree.get(str(row["from"]), 0) + 1
    for corridor_id in sorted(edges):
        edge = edges[corridor_id]
        if edge.ceiling_m_agl - edge.floor_m_agl < edge.width_m // 10:
            raise ValueError(f"{corridor_id}: band floor/ceiling is inconsistent")
    closures = tuple(
        Closure(
            corridor_id=_fictional(str(row["corridor_id"]), "closure target"),
            from_us=int(row["from_minute"]) * MINUTE_US,
            to_us=int(row["to_minute"]) * MINUTE_US,
            reason=str(row.get("reason", "synthetic")),
        )
        for row in sorted(
            doc.get("closures", []),
            key=lambda item: (str(item["corridor_id"]), int(item["from_minute"])),
        )
    )
    # A merge node is anywhere traffic converges into, or diverges out of, more than
    # one lane; entry is metered by the separation buffer at these nodes.
    merge_nodes = frozenset(
        node for node in facilities if in_degree.get(node, 0) >= 2 or out_degree.get(node, 0) >= 2
    )
    return edges, closures, merge_nodes


def build_weather(doc: dict[str, Any]) -> tuple[WeatherTracePoint, ...]:
    _check_synthetic(doc, "weather.yaml")
    trace = tuple(
        WeatherTracePoint(
            minute=int(row["minute"]),
            wind_ms=int(row["wind_ms"]),
            headwind_bp=int(row.get("headwind_bp", 0)),
        )
        for row in sorted(doc.get("trace", []), key=lambda item: int(item["minute"]))
    )
    if not trace or trace[0].minute != 0:
        raise ValueError("weather.yaml must declare a frozen trace starting at minute 0")
    return trace


def build_orders(
    doc: dict[str, Any], family: str, facilities: dict[str, Facility]
) -> tuple[Order, ...]:
    _check_synthetic(doc, "demand.yaml")
    if family not in {"routine", "rush", "failure"}:
        raise ValueError(f"unknown demand family {family!r}")
    orders: list[Order] = []
    for row in sorted(doc.get("orders", []), key=lambda item: str(item["order_id"])):
        if str(row.get("family", "routine")) != family:
            continue
        order = Order(
            order_id=str(row["order_id"]),
            family=family,
            arrival_minute=int(row["arrival_minute"]),
            origin_facility_id=str(row["origin_facility_id"]),
            destination_facility_id=str(row["destination_facility_id"]),
            priority_class=str(row.get("priority_class", "routine")),
            payload_g=int(row.get("payload_g", 500)),
            deadline_minute=int(row["deadline_minute"]),
            partner_id=_fictional(
                str(row.get("partner_id", "partner-unspecified-synthetic")), "partner"
            ),
        )
        if (
            order.origin_facility_id not in facilities
            or order.destination_facility_id not in facilities
        ):
            raise ValueError(f"{order.order_id}: unknown facility endpoint")
        orders.append(order)
    if not orders:
        raise ValueError(f"demand.yaml declares no {family} orders")
    return tuple(orders)


def build_faults(doc: dict[str, Any]) -> tuple[Fault, ...]:
    _check_synthetic(doc, "faults.yaml")
    kinds = {"telemetry_loss", "aircraft_stall", "weather_window_close", "facility_outage"}
    faults = []
    for row in sorted(doc.get("faults", []), key=lambda item: str(item["fault_id"])):
        kind = str(row["type"])
        if kind not in kinds:
            raise ValueError(f"{row['fault_id']}: unknown fault type {kind!r}")
        faults.append(
            Fault(
                fault_id=str(row["fault_id"]),
                kind=kind,
                at_minute=int(row["at_minute"]),
                target_id=str(row.get("target_id", "")),
                until_minute=int(row.get("until_minute", 0)),
            )
        )
    return tuple(faults)


def build_spec(
    documents: dict[str, Any],
    scenario_id: str,
    seed: int,
    horizon_minutes: int,
    family: str,
) -> WorldSpec:
    policies_doc = documents["policies.yaml"]
    _check_synthetic(policies_doc, "policies.yaml")
    runtime = documents["runtime"]
    horizon_us = horizon_minutes * MINUTE_US
    facilities = build_facilities(documents["facilities.yaml"])
    profiles = build_profiles(documents["fleet.yaml"])
    edges, closures, merge_nodes = build_corridors(documents["corridors.yaml"], facilities)
    policies = {
        "separation_buffer_us": int(policies_doc["separation_buffer_us"]),
        "link_loss_rate_bp": int(policies_doc["link_loss_rate_bp"]),
        "max_agl_m": int(policies_doc["max_agl_m"]),
        "wind_jitter_ms": int(policies_doc.get("wind_jitter_ms", 1)),
        "battery_noise_spread_bp": int(policies_doc.get("battery_noise_spread_bp", 300)),
        "wind_energy_bp_per_ms": int(policies_doc.get("wind_energy_bp_per_ms", 250)),
        "charge_target_pct": int(policies_doc.get("charge_target_pct", 90)),
        "reposition_max_us": int(policies_doc.get("reposition_max_min", 20)) * MINUTE_US,
        "stall_grounding_us": int(policies_doc.get("stall_grounding_min", 20)) * MINUTE_US,
    }
    cost_model = {
        key: int(value)
        for key, value in sorted(policies_doc.get("cost_model", {}).items())
        if isinstance(value, int)
    }
    return WorldSpec(
        scenario_id=scenario_id,
        seed=seed,
        horizon_us=horizon_us,
        snapshot_interval_us=int(runtime.get("snapshot_interval_min", 10)) * MINUTE_US,
        policies=policies,
        profiles=profiles,
        aircraft=build_aircraft(documents["fleet.yaml"], profiles),
        facilities=facilities,
        edges=edges,
        closures=closures,
        merge_nodes=merge_nodes,
        wind_trace=build_weather(documents["weather.yaml"]),
        orders=build_orders(documents["demand.yaml"], family, facilities),
        faults=build_faults(documents["faults.yaml"]),
        demand_jitter_us=int(policies_doc.get("demand_jitter_s", 30)) * MICROSECONDS,
        charge_ready_pct=int(policies_doc.get("charge_ready_pct", 35)),
        cost_model=cost_model,
    )
