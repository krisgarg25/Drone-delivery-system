"""Build the Phase 1 scenario bundles (deterministic, one-shot, stdlib + PyYAML).

Run: `uv run --no-sync python scripts/build_phase1_bundles.py`

This script exists so the bundles in `simulation/scenarios/` are reproducible from a
single reviewed recipe: geometry, capacities, fleet states, demand families and faults
are all emitted here in sorted order from fixed seeds, then frozen by `droneops-sim
freeze`. Regenerating must produce byte-identical files; the generated bundles, not
this script, are the deterministic input to runs.

Two topology shapes ship, because the spec (four hubs, six nodes) and the demo chapter
(two hubs, eight nodes) disagree and the engine is data-driven:

- `chandigarh-synthetic-v1`: 4 hubs, 6 nodes, 30 registered aircraft, 3 demand families.
- `chandigarh-basic`:        2 hubs, 8 nodes, 6 aircraft, the Phase 0 orders.

All names are fictional. All values illustrative. Simulation only.
"""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parent.parent
SCENARIOS = REPO / "simulation" / "scenarios"
MINUTE = 1_000_000 * 60

EPOCH = "2026-09-30T06:00:00Z"
EXTRACT_DATE = "2026-09-20"
BOUNDARY_RELATION = 1942809
PARTNERS = {
    "node-medlab-synthetic": ("partner-medlab-synthetic", "routine"),
    "node-quickcart-synthetic": ("partner-quickcart-synthetic", "commercial_sla"),
    "node-sparesdepot-synthetic": ("partner-sparesdepot-synthetic", "routine"),
}


def dump(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(payload, sort_keys=True, allow_unicode=True, width=110),
        encoding="utf-8",
        newline="\n",
    )


def _geo(lat_e6: int, lon_e6: int, alt_m: int) -> dict[str, Any]:
    return {"lat_e6": lat_e6, "lon_e6": lon_e6, "alt_m_agl": alt_m, "altitude_reference": "AGL"}


def facility(  # noqa: PLR0917 - generator helper, not a public API
    fid: str, kind: str, name: str, lat: int, lon: int, pads: int, bays: int, **extra: Any
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "facility_id": fid,
        "kind": kind,
        "name": name,
        "location": _geo(lat, lon, 320),
        "pads": pads,
        "charger_bays": bays,
        "charge_power_w": 900 if kind == "node" else 1200,
        "loading_turnaround_min": 2,
        "offload_turnaround_min": 1,
        "open_minute": 0,
        "close_minute": 60,
        "crew_shifts": [[0, 60]],
    }
    row.update(extra)
    return row


def lane(  # noqa: PLR0917 - generator helper, not a public API
    cid: str,
    origin: str,
    dest: str,
    length_m: int,
    band: str,
    wind: int,
    rng: random.Random,
    alt: int,
) -> dict[str, Any]:
    lat1, lon1 = POINTS[origin]
    lat2, lon2 = POINTS[dest]
    mid_lat = (lat1 + lat2) // 2 + rng.randint(-2000, 2000)
    mid_lon = (lon1 + lon2) // 2 + rng.randint(-2000, 2000)
    return {
        "corridor_id": cid,
        "from": origin,
        "to": dest,
        "length_m": length_m,
        "width_m": 100,
        "band": band,
        "max_wind_ms": wind,
        "synthetic": True,
        "geometry": [_geo(lat1, lon1, alt), _geo(mid_lat, mid_lon, alt), _geo(lat2, lon2, alt)],
    }


POINTS: dict[str, tuple[int, int]] = {}


def register(rows: list[dict[str, Any]]) -> None:
    for row in rows:
        POINTS[row["facility_id"]] = (row["location"]["lat_e6"], row["location"]["lon_e6"])


def corridor_doc(edges: list[dict[str, Any]], closures: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "authoritative": False,
        "altitude_reference": "AGL",
        "source": "synthetic road and green-space heuristic (technical-build/12 section 7)",
        "safety_claim": False,
        "bands": [
            {"band": "A", "floor_m_agl": 30, "ceiling_m_agl": 50, "speed_cap_mps": 14},
            {"band": "B", "floor_m_agl": 50, "ceiling_m_agl": 70, "speed_cap_mps": 16},
            {"band": "C", "floor_m_agl": 70, "ceiling_m_agl": 90, "speed_cap_mps": 18},
        ],
        "edges": sorted(edges, key=lambda row: str(row["corridor_id"])),
        "closures": closures,
    }


def policies_doc(**extra: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "schema_version": 1,
        "authoritative": False,
        "policy_version": "chandigarh-synthetic-p1",
        "epoch": EPOCH,
        "source": "configs/base/safety.yaml",
        "energy_reserve_fraction_bp": 2500,
        "separation_buffer_us": 2 * MINUTE,
        "link_loss_rate_bp": 500,
        "altitude_reference_required": "AGL",
        "max_agl_m": 90,
        "demand_jitter_s": 30,
        "wind_jitter_ms": 1,
        "battery_noise_spread_bp": 300,
        "wind_energy_bp_per_ms": 250,
        "charge_target_pct": 90,
        "charge_ready_pct": 70,
        "charge_derate_bp": 11000,
        "reposition_max_min": 20,
        "stall_grounding_min": 20,
        # Illustrative economics only (05-chandigarh-demo.md section 5.9): unvalidated,
        # never a price quote. Integer paise.
        "cost_model": {
            "hub_cost_per_day_paise": 1_800_000,
            "aircraft_cost_per_day_paise": 9_000,
            "staff_cost_per_day_paise": 6_000_000,
            "insurance_cost_per_day_paise": 2_500_000,
            "energy_cost_per_kwh_paise": 800,
            "failed_mission_cost_paise": 150_000,
        },
    }
    doc.update(extra)
    return doc


def weather_doc(trace: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "authoritative": False,
        "source": "synthetic frozen trace (no live fetch during a deterministic run)",
        "retrieved_at": "2026-09-30T05:00:00Z",
        "expires_at": "2026-10-07T06:00:00Z",
        "freshness": "frozen_bundle",
        "station": "IXC-synthetic",
        "headwind_bp": 1200,
        "trace": trace,
    }


def wind_trace(spike_lo: int, spike_hi: int, spike_ms: int) -> list[dict[str, Any]]:
    rows = []
    for minute in range(60):
        base = 5 + (2 if (minute // 5) % 2 else 0) + (1 if minute % 7 == 0 else 0)
        if spike_lo <= minute <= spike_hi:
            base = spike_ms
        rows.append({"minute": minute, "wind_ms": base, "headwind_bp": 1200})
    return rows


DESTINATIONS: dict[str, dict[str, list[str]]] = {
    # Plausible single-hop destinations per partner site; nothing here assumes a
    # cross-city flight on one battery.
    "routine": {
        "node-medlab-synthetic": ["hub-north-synthetic", "hub-central-synthetic"],
        "node-quickcart-synthetic": ["hub-central-synthetic", "hub-south-campus-synthetic"],
        "node-sparesdepot-synthetic": ["hub-ridge-east-synthetic", "hub-south-campus-synthetic"],
    },
    "rush": {
        "node-medlab-synthetic": ["hub-central-synthetic", "hub-north-synthetic"],
        "node-quickcart-synthetic": ["hub-south-campus-synthetic", "hub-central-synthetic"],
        "node-sparesdepot-synthetic": ["hub-south-campus-synthetic", "hub-ridge-east-synthetic"],
    },
    "failure": {
        "node-medlab-synthetic": ["hub-north-synthetic", "hub-central-synthetic"],
        "node-quickcart-synthetic": ["hub-central-synthetic"],
        "node-sparesdepot-synthetic": ["hub-ridge-east-synthetic"],
    },
}


def order_rows(
    rng: random.Random, family: str, count: int, deadline_min: int
) -> list[dict[str, Any]]:
    partners = sorted(PARTNERS)
    rows = []
    for index in range(count):
        minute = (index * 60) // count
        origin = partners[index % len(partners)]
        destinations = DESTINATIONS[family][origin]
        destination = destinations[(index // len(partners)) % len(destinations)]
        priority = "routine"
        roll = rng.randrange(0, 100)
        if roll < 8:
            priority = "commercial_sla"
        elif roll < 10:
            priority = "life_critical"
        rows.append(
            {
                "order_id": f"ord-{family[:2]}{index:03d}",
                "family": family,
                "arrival_minute": minute,
                "origin_facility_id": origin,
                "destination_facility_id": destination,
                "priority_class": priority,
                "payload_g": 500 + 250 * (index % 5),
                "deadline_minute": minute + deadline_min,
                "partner_id": PARTNERS[origin][0],
            }
        )
        if priority == "life_critical":
            rows[-1]["payload_g"] = 500
    return rows


def fleet_v1() -> dict[str, Any]:
    profiles = [
        {
            "profile_id": "airframe-x-sim",
            "source": "contracts/vehicle-profile/airframe-x-sim.yaml",
            "illustrative": True,
            "max_payload_g": 2000,
            "cruise_speed_mps": 12,
            "capacity_wh": 400,
            "reserve_fraction_bp": 2500,
            "charge_power_w": 900,
            "battery_curve": [
                {"payload_g": 0, "wh_per_100km": 1_200},
                {"payload_g": 1000, "wh_per_100km": 1_500},
                {"payload_g": 2000, "wh_per_100km": 1_900},
            ],
        },
        {
            "profile_id": "airframe-y-sim",
            "source": "contracts/vehicle-profile/airframe-y-sim.yaml",
            "illustrative": True,
            "max_payload_g": 3500,
            "cruise_speed_mps": 10,
            "capacity_wh": 650,
            "reserve_fraction_bp": 3000,
            "charge_power_w": 1200,
            "battery_curve": [
                {"payload_g": 0, "wh_per_100km": 2_000},
                {"payload_g": 1750, "wh_per_100km": 2_400},
                {"payload_g": 3500, "wh_per_100km": 2_800},
            ],
        },
    ]
    homes = [
        # Six aircraft start pre-positioned at the three partner sites (two each);
        # the remaining fleet is distributed across the four hubs.
        "node-medlab-synthetic",
        "node-quickcart-synthetic",
        "node-sparesdepot-synthetic",
        "node-medlab-synthetic",
        "node-quickcart-synthetic",
        "node-sparesdepot-synthetic",
        "hub-central-synthetic",
        "hub-north-synthetic",
        "hub-south-campus-synthetic",
        "hub-ridge-east-synthetic",
    ]
    # 30 registered: 12 available, 8 charging, 4 maintenance, 6 offline (05-chandigarh-demo.md 5.3)
    states: list[tuple[str, int]] = (
        [("available", 0)] * 12
        + [("charging", 0)] * 8
        + [("maintenance", 0)] * 4
        + [("offline", 90)] * 6  # offline never returns inside the horizon
    )
    rng = random.Random(530_913)
    aircraft = []
    for index in range(30):
        status, available_at = states[index]
        if status == "charging":
            available_at = 0  # engine back-derives the bay time from the charge curve
        charge = {"available": rng.choice([85, 92, 100]), "charging": rng.choice([75, 80, 85])}.get(
            status, rng.choice([55, 60, 70])
        )
        aircraft.append(
            {
                "aircraft_id": f"air-{index + 1:02d}-synthetic",
                "profile_id": "airframe-y-sim" if index % 5 == 0 else "airframe-x-sim",
                "home_facility_id": homes[index % len(homes)],
                "status": status,
                "charge_pct": charge,
                "available_at_minute": available_at,
                "reposition_enabled": True,
            }
        )
    return {
        "schema_version": 1,
        "authoritative": False,
        "profiles_note": "Illustrative battery curves only. Not certified aircraft data.",
        "profiles": profiles,
        "aircraft": aircraft,
    }


def build_v1() -> Path:
    directory = SCENARIOS / "chandigarh-synthetic-v1"
    rng = random.Random(202_609_301)
    facilities = [
        facility(
            "hub-central-synthetic",
            "hub",
            "Central Ridge Staging Hub (fictional)",
            30_738_300,
            76_793_600,
            4,
            6,
        ),
        facility(
            "hub-north-synthetic",
            "hub",
            "North Perimeter Staging Hub (fictional)",
            30_766_000,
            76_785_000,
            3,
            4,
        ),
        facility(
            "hub-south-campus-synthetic",
            "hub",
            "South Campus Staging Hub (fictional)",
            30_670_000,
            76_780_000,
            3,
            4,
        ),
        facility(
            "hub-ridge-east-synthetic",
            "hub",
            "East Ridge Staging Hub (fictional)",
            30_745_000,
            76_830_000,
            2,
            3,
            open_minute=4,
            outage={"state": "closed", "from_minute": 41, "to_minute": 49},
        ),
        facility(
            "node-medlab-synthetic",
            "node",
            "Fictional Medical Parcel Node A",
            30_752_000,
            76_770_000,
            2,
            2,
        ),
        facility(
            "node-quickcart-synthetic",
            "node",
            "Fictional Express Retail Node B",
            30_700_000,
            76_760_000,
            2,
            1,
        ),
        facility(
            "node-sparesdepot-synthetic",
            "node",
            "Fictional Industrial Spares Node C",
            30_720_000,
            76_840_000,
            2,
            1,
        ),
        facility(
            "node-rimroad-synthetic",
            "node",
            "Fictional Rim Road Handover D",
            30_680_000,
            76_820_000,
            1,
            1,
        ),
        facility(
            "node-greenpark-synthetic",
            "node",
            "Fictional Green Park Handover E",
            30_760_000,
            76_810_000,
            1,
            1,
        ),
        facility(
            "node-lakeview-synthetic",
            "node",
            "Fictional Lakeview Handover F",
            30_735_000,
            76_750_000,
            1,
            1,
            crew_shifts=[[0, 25], [30, 60]],
        ),
    ]
    register(facilities)
    pairs = [
        ("hub-central-synthetic", "hub-north-synthetic", 3_600, "B", 11),
        ("hub-central-synthetic", "hub-south-campus-synthetic", 7_800, "C", 10),
        ("hub-central-synthetic", "hub-ridge-east-synthetic", 4_100, "A", 11),
        ("hub-north-synthetic", "node-rimroad-synthetic", 5_200, "C", 10),
        ("hub-south-campus-synthetic", "node-greenpark-synthetic", 6_400, "B", 9),
        ("hub-south-campus-synthetic", "node-sparesdepot-synthetic", 8_900, "C", 10),
        ("hub-ridge-east-synthetic", "node-sparesdepot-synthetic", 5_400, "A", 9),
        ("hub-central-synthetic", "node-medlab-synthetic", 4_600, "B", 10),
        ("hub-north-synthetic", "node-medlab-synthetic", 4_900, "A", 11),
        ("hub-central-synthetic", "node-quickcart-synthetic", 6_800, "A", 9),
        ("hub-south-campus-synthetic", "node-quickcart-synthetic", 6_100, "B", 10),
        ("hub-ridge-east-synthetic", "node-lakeview-synthetic", 7_200, "C", 11),
        ("hub-north-synthetic", "node-lakeview-synthetic", 6_600, "B", 10),
    ]
    edges = []
    for origin, dest, length, band, wind in pairs:
        slug_o = origin.replace("-synthetic", "").replace("hub-", "h").replace("node-", "n")
        slug_d = dest.replace("-synthetic", "").replace("hub-", "h").replace("node-", "n")
        edges.append(
            lane(
                f"lane-{slug_o}-{slug_d}-{band}-synthetic",
                origin,
                dest,
                length,
                band,
                wind,
                rng,
                {"A": 40, "B": 60, "C": 85}[band],
            )
        )
        back = band if band != "A" else "B"
        edges.append(
            lane(
                f"lane-{slug_d}-{slug_o}-{back}-synthetic",
                dest,
                origin,
                length + 400,
                back,
                wind,
                rng,
                {"A": 40, "B": 60, "C": 85}[back],
            )
        )
    closures = [
        {
            "corridor_id": "lane-hcentral-hsouth-campus-C-synthetic",
            "from_minute": 30,
            "to_minute": 38,
            "reason": "synthetic crane survey overlay",
        },
        {
            "corridor_id": "lane-hsouth-campus-hcentral-C-synthetic",
            "from_minute": 30,
            "to_minute": 38,
            "reason": "synthetic crane survey overlay",
        },
    ]
    routine = order_rows(rng, "routine", 60, 25)
    rush = order_rows(rng, "rush", 120, 18)
    failure = order_rows(rng, "failure", 58, 14)
    # Duplicate submissions: same order_id arrives twice (5.7 acceptance test).
    failure.append(
        {
            **failure[4],
            "arrival_minute": failure[4]["arrival_minute"] + 4,
            "deadline_minute": failure[4]["deadline_minute"] + 4,
        }
    )
    failure.append(
        {
            **failure[9],
            "arrival_minute": failure[9]["arrival_minute"] + 7,
            "deadline_minute": failure[9]["deadline_minute"] + 7,
        }
    )
    failure.append(
        {
            **failure[15],
            "arrival_minute": failure[15]["arrival_minute"] + 2,
            "deadline_minute": failure[15]["deadline_minute"] + 2,
        }
    )
    orders = sorted(
        routine + rush + failure, key=lambda row: (str(row["order_id"]), int(row["arrival_minute"]))
    )
    faults = [
        {
            "fault_id": "flt-telemetry-01",
            "type": "telemetry_loss",
            "at_minute": 20,
            "target_id": routine[6]["order_id"],
        },
        {
            "fault_id": "flt-telemetry-02",
            "type": "telemetry_loss",
            "at_minute": 44,
            "target_id": rush[31]["order_id"],
        },
        {
            "fault_id": "flt-stall-01",
            "type": "aircraft_stall",
            "at_minute": 24,
            "target_id": "air-02-synthetic",
        },
        {
            "fault_id": "flt-stall-02",
            "type": "aircraft_stall",
            "at_minute": 47,
            "target_id": "air-11-synthetic",
        },
        {
            "fault_id": "flt-weather-01",
            "type": "weather_window_close",
            "at_minute": 30,
            "target_id": "lane-hcentral-hsouth-campus-C-synthetic",
            "until_minute": 38,
        },
        {
            "fault_id": "flt-outage-01",
            "type": "facility_outage",
            "at_minute": 41,
            "target_id": "hub-ridge-east-synthetic",
            "until_minute": 49,
        },
    ]
    dump(
        directory / "facilities.yaml",
        {"schema_version": 1, "authoritative": False, "facilities": facilities},
    )
    dump(directory / "corridors.yaml", corridor_doc(edges, closures))
    dump(directory / "fleet.yaml", fleet_v1())
    dump(
        directory / "demand.yaml",
        {
            "schema_version": 1,
            "authoritative": False,
            "partners": [
                {"partner_id": value[0], "site": site, "sector": sector, "fictional": True}
                for (site, (value, sector)) in sorted(PARTNERS.items())
            ],
            "families": {"routine": 60, "rush": 120, "failure": 61},
            "orders": orders,
        },
    )
    dump(directory / "weather.yaml", weather_doc(wind_trace(33, 41, 12)))
    dump(directory / "faults.yaml", {"schema_version": 1, "authoritative": False, "faults": faults})
    dump(directory / "policies.yaml", policies_doc())
    dump(
        directory / "scenario.yaml",
        {
            "schema_version": 1,
            "scenario_id": "chandigarh-synthetic-v1",
            "authoritative": False,
            "title": (
                "Chandigarh synthetic world: four hubs, six nodes, three partner sites, "
                "seeded demand families"
            ),
            "topology": {"hubs": 4, "nodes": 6},
            "human_expectation": (
                "Every committed mission holds protected corridor volumes no other committed "
                "mission overlaps; capacity queues rather than overbooks; offline and "
                "maintenance aircraft never fly; every rejected request is counted with a "
                "reason code; the report shows completed, late, unserved, energy, duration, "
                "waits and an illustrative cost per delivery."
            ),
            "what_this_does_not_prove": (
                "Nothing about real Chandigarh airspace, aircraft performance, weather or "
                "certification. Boundary geometry is licensed context; everything vertical "
                "is synthetic and requires survey."
            ),
            "boundary": {
                "source": "OpenStreetMap",
                "relation_id": BOUNDARY_RELATION,
                "license": "ODbL",
                "attribution": "(c) OpenStreetMap contributors",
                "extract_date": EXTRACT_DATE,
            },
            "synthetic_flags": {
                "facilities": True,
                "corridors": True,
                "fleet": True,
                "demand": True,
                "weather": True,
                "obstacles_require_survey": True,
            },
        },
    )
    dump(
        directory / "bundle.yaml",
        {
            "schema_version": 1,
            "scenario_id": "chandigarh-synthetic-v1",
            "authoritative": False,
            "seed": 202_609_300,
            "runtime": {
                "horizon_minutes": 60,
                "snapshot_interval_min": 10,
                "demand_family": "routine",
                "timezone": "UTC",
                "locale": "C",
                "solver_worker_count": 1,
                "policy_version": "chandigarh-synthetic-p1",
            },
            "files": {},
            "source_manifest": source_manifest(),
        },
    )
    return directory


def source_manifest() -> dict[str, Any]:
    return {
        "boundary": {
            "source": "OpenStreetMap",
            "relation_id": BOUNDARY_RELATION,
            "extract_date": EXTRACT_DATE,
            "query_hash": "sha256:pending-etl-task-4",
            "license": "ODbL",
            "attribution": "(c) OpenStreetMap contributors",
            "access_policy": (
                "user agent + retry + backoff + local cache; never required for a deterministic run"
            ),
        },
        "osm_features": {
            "source": "OpenStreetMap",
            "license": "ODbL",
            "attribution": "(c) OpenStreetMap contributors",
            "extract_date": EXTRACT_DATE,
            "building_height_coverage": "1 tagged height of ~33,351 building ways",
        },
        "terrain": {
            "source": "none (flat synthetic reference)",
            "version": "phase-1-none",
            "vertical_datum": "UNSPECIFIED",
        },
        "weather": {
            "source": "synthetic",
            "expires_at": "2026-10-07T06:00:00Z",
            "retrieved_at": EPOCH,
            "live_fetch": "forbidden",
        },
        "restrictions": {
            "source": "synthetic",
            "authoritative": False,
            "expires_at": "2026-10-07T06:00:00Z",
        },
        "obstacles": {"synthetic": True, "requires_survey": True, "vertical_authority": "none"},
        "synthetic_flags": {
            "corridors": True,
            "demand": True,
            "facilities": True,
            "fleet": True,
            "weather": True,
            "obstacles_require_survey": True,
        },
    }


def build_basic() -> Path:
    directory = SCENARIOS / "chandigarh-basic"
    rng = random.Random(202_609_302)
    facilities = [
        facility(
            "depot-north-synthetic", "hub", "Fictional North Depot", 30_756_000, 76_780_000, 2, 3
        ),
        facility(
            "depot-south-synthetic", "hub", "Fictional South Depot", 30_678_000, 76_790_000, 2, 3
        ),
        facility(
            "pad-central-synthetic",
            "node",
            "Fictional Central Handover",
            30_735_000,
            76_790_000,
            1,
            1,
        ),
        facility(
            "pad-campus-synthetic",
            "node",
            "Fictional Campus Handover",
            30_690_000,
            76_760_000,
            1,
            1,
        ),
        facility(
            "pad-ridge-synthetic", "node", "Fictional Ridge Handover", 30_745_000, 76_825_000, 1, 1
        ),
        facility(
            "pad-market-synthetic",
            "node",
            "Fictional Market Handover",
            30_728_000,
            76_800_000,
            1,
            0,
        ),
        facility(
            "pad-industrial-synthetic",
            "node",
            "Fictional Industrial Handover",
            30_683_000,
            76_810_000,
            1,
            1,
        ),
        facility(
            "pad-riverside-synthetic",
            "node",
            "Fictional Riverside Handover",
            30_762_000,
            76_805_000,
            1,
            0,
        ),
        facility(
            "pad-stadium-synthetic",
            "node",
            "Fictional Stadium Handover",
            30_710_000,
            76_820_000,
            1,
            1,
        ),
        facility(
            "pad-airfield-synthetic",
            "node",
            "Fictional Airfield Perimeter Handover",
            30_748_000,
            76_765_000,
            1,
            0,
        ),
    ]
    register(facilities)
    pairs = [
        ("depot-north-synthetic", "pad-central-synthetic", 2_600, "B"),
        ("depot-south-synthetic", "pad-campus-synthetic", 3_100, "A"),
        ("pad-central-synthetic", "pad-ridge-synthetic", 4_900, "C"),
        ("depot-north-synthetic", "pad-riverside-synthetic", 3_700, "A"),
        ("depot-south-synthetic", "pad-industrial-synthetic", 4_400, "B"),
        ("depot-south-synthetic", "pad-market-synthetic", 5_600, "B"),
        ("pad-central-synthetic", "pad-market-synthetic", 1_800, "A"),
        ("pad-market-synthetic", "pad-stadium-synthetic", 3_900, "B"),
        ("depot-south-synthetic", "pad-airfield-synthetic", 8_300, "C"),
        ("pad-campus-synthetic", "pad-industrial-synthetic", 6_200, "B"),
        ("pad-stadium-synthetic", "pad-ridge-synthetic", 5_100, "C"),
    ]
    edges = []
    for origin, dest, length, band in pairs:
        slug_o = origin.split("-")[1]
        slug_d = dest.split("-")[1]
        edges.append(
            lane(
                f"lane-{slug_o}-{slug_d}-{band}-synthetic",
                origin,
                dest,
                length,
                band,
                11,
                rng,
                {"A": 40, "B": 60, "C": 85}[band],
            )
        )
        edges.append(
            lane(
                f"lane-{slug_d}-{slug_o}-{band}-synthetic",
                dest,
                origin,
                length,
                band,
                11,
                rng,
                {"A": 40, "B": 60, "C": 85}[band],
            )
        )
    orders: list[dict[str, Any]] = []
    legacy = [
        ("ord-0001", 10, "depot-north-synthetic", "pad-central-synthetic", "routine", 25),
        ("ord-0002", 12, "depot-south-synthetic", "pad-campus-synthetic", "commercial_sla", 25),
        ("ord-0003", 30, "depot-north-synthetic", "pad-central-synthetic", "life_critical", 20),
        ("ord-0004", 31, "depot-north-synthetic", "pad-central-synthetic", "routine", 25),
        ("ord-0005", 45, "depot-south-synthetic", "pad-ridge-synthetic", "routine", 25),
        ("ord-0006", 52, "depot-south-synthetic", "pad-ridge-synthetic", "safety_contingency", 20),
    ]
    for order_id, minute, origin, dest, priority, deadline in legacy:
        orders.append(
            {
                "order_id": order_id,
                "family": "routine",
                "arrival_minute": minute,
                "origin_facility_id": origin,
                "destination_facility_id": dest,
                "priority_class": priority,
                "payload_g": 500,
                "deadline_minute": minute + deadline,
                "partner_id": "partner-basic-synthetic",
            }
        )
    aircraft = [
        {
            "aircraft_id": "air-01-synthetic",
            "profile_id": "airframe-x-sim",
            "home_facility_id": "depot-north-synthetic",
            "status": "available",
            "charge_pct": 100,
            "available_at_minute": 0,
        },
        {
            "aircraft_id": "air-02-synthetic",
            "profile_id": "airframe-x-sim",
            "home_facility_id": "depot-north-synthetic",
            "status": "charging",
            "charge_pct": 82,
            "available_at_minute": 0,
        },
        {
            "aircraft_id": "air-03-synthetic",
            "profile_id": "airframe-y-sim",
            "home_facility_id": "depot-south-synthetic",
            "status": "available",
            "charge_pct": 95,
            "available_at_minute": 0,
        },
        {
            "aircraft_id": "air-04-synthetic",
            "profile_id": "airframe-y-sim",
            "home_facility_id": "depot-south-synthetic",
            "status": "maintenance",
            "charge_pct": 60,
            "available_at_minute": 26,
        },
        {
            "aircraft_id": "air-05-synthetic",
            "profile_id": "airframe-x-sim",
            "home_facility_id": "depot-north-synthetic",
            "status": "offline",
            "charge_pct": 90,
            "available_at_minute": 90,
        },
        {
            "aircraft_id": "air-06-synthetic",
            "profile_id": "airframe-y-sim",
            "home_facility_id": "depot-south-synthetic",
            "status": "available",
            "charge_pct": 88,
            "available_at_minute": 0,
        },
    ]
    profiles = fleet_v1()["profiles"]
    dump(
        directory / "facilities.yaml",
        {"schema_version": 1, "authoritative": False, "facilities": facilities},
    )
    dump(directory / "corridors.yaml", corridor_doc(edges, []))
    dump(
        directory / "fleet.yaml",
        {
            "schema_version": 1,
            "authoritative": False,
            "profiles_note": "Illustrative battery curves only. Not certified aircraft data.",
            "profiles": profiles,
            "aircraft": aircraft,
        },
    )
    dump(
        directory / "demand.yaml",
        {
            "schema_version": 1,
            "authoritative": False,
            "partners": [
                {
                    "partner_id": "partner-basic-synthetic",
                    "sector": "fictional general",
                    "fictional": True,
                }
            ],
            "families": {"routine": 6},
            "orders": sorted(orders, key=lambda row: str(row["order_id"])),
        },
    )
    dump(directory / "weather.yaml", weather_doc(wind_trace(999, 999, 5)))
    dump(directory / "faults.yaml", {"schema_version": 1, "authoritative": False, "faults": []})
    dump(directory / "policies.yaml", policies_doc(link_loss_rate_bp=3500))
    dump(
        directory / "scenario.yaml",
        {
            "schema_version": 1,
            "scenario_id": "chandigarh-basic",
            "authoritative": False,
            "title": "Chandigarh synthetic golden run: two hubs, eight nodes, corridor contention",
            "topology": {"hubs": 2, "nodes": 8},
            "human_expectation": (
                "Every committed mission holds a protected corridor volume that no other "
                "committed mission overlaps, no mission is accepted without its energy "
                "reserve, and every rejected request is counted rather than dropped."
            ),
            "what_this_does_not_prove": (
                "Nothing about real Chandigarh airspace, aircraft performance, weather or "
                "certification. Geometry is synthetic and obstacle heights in the wider "
                "dataset require survey."
            ),
            "boundary": {
                "source": "OpenStreetMap",
                "relation_id": BOUNDARY_RELATION,
                "license": "ODbL",
                "attribution": "(c) OpenStreetMap contributors",
                "extract_date": EXTRACT_DATE,
            },
            "synthetic_flags": {
                "facilities": True,
                "corridors": True,
                "fleet": True,
                "demand": True,
                "weather": True,
                "obstacles_require_survey": True,
            },
        },
    )
    dump(
        directory / "bundle.yaml",
        {
            "schema_version": 1,
            "scenario_id": "chandigarh-basic",
            "authoritative": False,
            "seed": 20_260_930,
            "runtime": {
                "horizon_minutes": 60,
                "snapshot_interval_min": 10,
                "demand_family": "routine",
                "timezone": "UTC",
                "locale": "C",
                "solver_worker_count": 1,
                "policy_version": "chandigarh-synthetic-p1",
            },
            "files": {},
            "source_manifest": source_manifest(),
        },
    )
    return directory


def main() -> None:
    for directory in (build_v1(), build_basic()):
        print(f"wrote {directory.relative_to(REPO)}")


if __name__ == "__main__":
    main()
