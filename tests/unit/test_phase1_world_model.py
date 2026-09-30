"""Phase 1 world-model gates: shapes, provenance, capacity queueing, metric arithmetic.

Everything here must pass with no database, no network and no wall clock: the same
pure kernel CI replays. Marked `golden` where it hashes bundle outputs.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from droneops_sim_core import REJECT_REASONS, Engine
from droneops_sim_core.model import (
    AircraftConfig,
    AircraftProfile,
    BatteryCurvePoint,
    Closure,
    CorridorEdge,
    Facility,
    GeometryPoint,
    Order,
    WeatherTracePoint,
    WorldSpec,
)
from droneops_sim_runner.bundle import load_bundle
from droneops_sim_runner.cli import main
from droneops_sim_runner.kernel import run_scenario, write_run
from droneops_sim_runner.world import build_spec

REPO_ROOT = Path(__file__).resolve().parents[2]
V1 = REPO_ROOT / "simulation" / "scenarios" / "chandigarh-synthetic-v1"
BASIC = REPO_ROOT / "simulation" / "scenarios" / "chandigarh-basic"

pytestmark = pytest.mark.golden


def _documents(path: Path) -> dict:
    bundle = load_bundle(path)
    documents = dict(bundle.documents)
    documents["runtime"] = bundle.manifest["runtime"]
    return documents


def _spec(path: Path, family: str, seed: int | None = None) -> WorldSpec:
    bundle = load_bundle(path)
    documents = _documents(path)
    return build_spec(
        documents,
        bundle.scenario_id,
        seed if seed is not None else bundle.seed,
        int(bundle.manifest["runtime"]["horizon_minutes"]),
        family,
    )


# ------------------------------------------------------------------ topology shapes
def test_both_topology_shapes_load_and_are_data_driven() -> None:
    """The spec says four hubs and six nodes; the demo chapter says two and eight.
    The engine is topology-agnostic, so both ship."""
    v1_fac = _documents(V1)["facilities.yaml"]["facilities"]
    basic_fac = _documents(BASIC)["facilities.yaml"]["facilities"]
    v1_kinds = sorted(row["kind"] for row in v1_fac)
    basic_kinds = sorted(row["kind"] for row in basic_fac)
    assert (v1_kinds.count("hub"), v1_kinds.count("node")) == (4, 6)
    assert (basic_kinds.count("hub"), basic_kinds.count("node")) == (2, 8)


def test_fleet_matches_the_demo_starting_state() -> None:
    aircraft = _documents(V1)["fleet.yaml"]["aircraft"]
    assert len(aircraft) == 30
    states: dict[str, int] = {}
    for row in aircraft:
        states[row["status"]] = states.get(row["status"], 0) + 1
    assert states == {"available": 12, "charging": 8, "maintenance": 4, "offline": 6}
    profiles = _documents(V1)["fleet.yaml"]["profiles"]
    assert len(profiles) == 2
    for profile in profiles:
        assert profile["battery_curve"], "battery curves must be explicit illustrative data"


def test_every_geometry_and_altitude_carries_its_reference() -> None:
    spec = _spec(V1, "routine")
    assert {edge.band for edge in spec.edges.values()} == {"A", "B", "C"}
    assert spec.closures, "the graph must ship closure overlays"
    assert spec.merge_nodes, "directed lanes must declare merge nodes"
    for edge in spec.edges.values():
        assert len(edge.geometry) >= 2
        for point in edge.geometry:
            assert point.altitude_reference == "AGL"
        assert edge.synthetic
        assert edge.source == "synthetic"
    for facility in spec.facilities.values():
        assert facility.location.altitude_reference == "AGL"
        assert facility.pad_count >= 1
        assert facility.crew_shifts


def test_a_geometry_without_reference_is_a_load_error(tmp_path: Path) -> None:
    documents = _documents(BASIC)
    edge = documents["corridors.yaml"]["edges"][0]
    edge["geometry"][0]["altitude_reference"] = "UNSPECIFIED"
    with pytest.raises(ValueError, match="must be AGL"):
        build_spec(documents, "test", 1, 60, "routine")


# ------------------------------------------------------------------ kernel invariants
def _minimal_spec(**overrides) -> WorldSpec:
    hub = Facility(
        facility_id="hub-a-synthetic",
        kind="hub",
        name="A",
        location=GeometryPoint(30_700_000, 76_800_000, 320),
        pad_count=1,
        charger_bays=2,
        charge_power_w=900,
        loading_turnaround_us=2 * 60 * 10**6,
        offload_turnaround_us=60 * 10**6,
        open_minute=0,
        close_minute=60,
        crew_shifts=((0, 60),),
    )
    node = Facility(
        facility_id="node-b-synthetic",
        kind="node",
        name="B",
        location=GeometryPoint(30_730_000, 76_830_000, 320),
        pad_count=2,
        charger_bays=2,
        charge_power_w=900,
        loading_turnaround_us=60 * 10**6,
        offload_turnaround_us=60 * 10**6,
        open_minute=0,
        close_minute=60,
        crew_shifts=((0, 60),),
    )
    hub_c = Facility(
        facility_id="node-c-synthetic",
        kind="node",
        name="C",
        location=GeometryPoint(30_690_000, 76_840_000, 320),
        pad_count=2,
        charger_bays=2,
        charge_power_w=900,
        loading_turnaround_us=60 * 10**6,
        offload_turnaround_us=60 * 10**6,
        open_minute=0,
        close_minute=60,
        crew_shifts=((0, 60),),
    )
    fwd = CorridorEdge(
        "lane-ab-synthetic",
        hub.facility_id,
        node.facility_id,
        3_000,
        100,
        "A",
        30,
        50,
        20,
        (GeometryPoint(30_700_000, 76_800_000, 40), GeometryPoint(30_730_000, 76_830_000, 40)),
    )
    back = CorridorEdge(
        "lane-ba-synthetic",
        node.facility_id,
        hub.facility_id,
        3_000,
        100,
        "A",
        30,
        50,
        20,
        (GeometryPoint(30_730_000, 76_830_000, 40), GeometryPoint(30_700_000, 76_800_000, 40)),
    )
    out_c = CorridorEdge(
        "lane-ac-synthetic",
        hub.facility_id,
        hub_c.facility_id,
        3_000,
        100,
        "B",
        30,
        50,
        20,
        (GeometryPoint(30_700_000, 76_800_000, 40), GeometryPoint(30_690_000, 76_840_000, 60)),
    )
    profile = AircraftProfile(
        "airframe-x-sim",
        2_000,
        12,
        400,
        2_500,
        900,
        (BatteryCurvePoint(0, 1_200), BatteryCurvePoint(2_000, 1_900)),
    )
    orders = overrides.pop("orders", None) or (
        Order("ord-a", "routine", 1, hub.facility_id, node.facility_id, "routine", 0, 30),
        Order("ord-b", "routine", 2, hub.facility_id, hub_c.facility_id, "routine", 0, 30),
    )
    return WorldSpec(
        scenario_id="test-world",
        seed=7,
        horizon_us=60 * 60 * 10**6,
        snapshot_interval_us=10 * 60 * 10**6,
        policies={
            "separation_buffer_us": 60 * 10**6,
            "link_loss_rate_bp": 0,
            "max_agl_m": 90,
            "wind_jitter_ms": 0,
            "battery_noise_spread_bp": 0,
            "wind_energy_bp_per_ms": 250,
            "charge_target_pct": 90,
            "reposition_max_us": 20 * 60 * 10**6,
            "stall_grounding_us": 20 * 60 * 10**6,
        },
        profiles={profile.profile_id: profile},
        aircraft=overrides.pop("aircraft", None)
        or (
            AircraftConfig(
                "air-01-synthetic", profile.profile_id, hub.facility_id, "available", 100, 0
            ),
            AircraftConfig(
                "air-02-synthetic", profile.profile_id, hub.facility_id, "available", 100, 0
            ),
        ),
        facilities={hub.facility_id: hub, node.facility_id: node, hub_c.facility_id: hub_c},
        edges={fwd.corridor_id: fwd, back.corridor_id: back, out_c.corridor_id: out_c},
        closures=overrides.pop("closures", None) or (),
        merge_nodes=frozenset({node.facility_id}),
        wind_trace=(WeatherTracePoint(0, 5, 1_200),),
        orders=orders,
        faults=(),
        demand_jitter_us=0,
        charge_ready_pct=70,
        cost_model={},
    )


def test_full_pads_queue_rather_than_overbook() -> None:
    result = Engine(_minimal_spec()).run()
    committed = [row for row in result.events if row["kind"] == "mission.committed"]
    assert len(committed) == 2, "both orders must still fly: capacity queues, it does not lose work"
    assert sorted(row["pad_wait_us"] for row in committed)[1] > 0, (
        "second mission must wait for the single pad"
    )


def test_offline_and_maintenance_aircraft_never_become_eligible() -> None:
    aircraft = (
        AircraftConfig(
            "air-01-synthetic", "airframe-x-sim", "hub-a-synthetic", "available", 100, 0
        ),
        AircraftConfig("air-02-synthetic", "airframe-x-sim", "hub-a-synthetic", "offline", 100, 90),
        AircraftConfig(
            "air-03-synthetic", "airframe-x-sim", "hub-a-synthetic", "maintenance", 100, 40
        ),
    )
    orders = tuple(
        Order(
            f"ord-{index}",
            "routine",
            index,
            "hub-a-synthetic",
            "node-b-synthetic",
            "routine",
            0,
            30,
        )
        for index in range(1, 8)
    )
    result = Engine(_minimal_spec(aircraft=aircraft, orders=orders)).run()
    flown = {row["aircraft_id"] for row in result.events if row["kind"] == "mission.committed"}
    assert flown == {"air-01-synthetic"}


def test_duplicate_submission_produces_one_mission_and_is_visible() -> None:
    result = Engine(_minimal_spec()).run()
    spec_orders = _spec(V1, "failure")
    failure = Engine(spec_orders).run()
    assert failure.metric_vector["orders_duplicate_rejected"] == 3
    assert failure.metric_vector["unserved"] == (
        failure.metric_vector["orders_rejected"]
        + failure.metric_vector["orders_unfinished_at_cutoff"]
        + failure.metric_vector["missions_failed"]
    )
    assert result.metric_vector["unserved"] >= 0


def test_closure_overlay_blocks_planned_traffic() -> None:
    spec = _minimal_spec(
        closures=(Closure("lane-ab-synthetic", 2 * 60 * 10**6, 30 * 60 * 10**6, "test"),)
    )
    result = Engine(spec).run()
    rejects = [row for row in result.events if row["kind"] == "order.rejected"]
    assert rejects
    assert all(row["reason"] in REJECT_REASONS for row in rejects)
    assert any(row["reason"] == "weather_window_closed" for row in rejects)


def test_lost_telemetry_holds_the_volume_as_uncertainty() -> None:
    result = Engine(_spec(V1, "routine")).run()
    lost = [row for row in result.events if row["kind"] == "telemetry.lost"]
    assert lost
    for row in lost:
        assert row["state"] == "protected_pending_reconciliation"
        assert row["airspace_released"] is False


def test_metric_vector_arithmetic_and_formulas() -> None:
    result = Engine(_spec(V1, "routine")).run()
    vector = result.metric_vector
    assert vector["orders_submitted"] == 60
    assert (
        vector["missions_completed"]
        + vector["orders_rejected"]
        + vector["orders_unfinished_at_cutoff"]
        + vector["missions_failed"]
        == vector["orders_submitted"]
    ), "submitted work must be fully accounted for, including rejected work"
    assert vector["invalid_committed_overlaps"] == 0
    assert vector["authorized_for_flight"] is False
    for row in result.metrics:
        assert row.formula, f"{row.metric_id} must state its formula"
        assert row.denominator, f"{row.metric_id} must state its denominator"


def test_seed_changes_decisions_but_never_breaks_invariants() -> None:
    first = Engine(_spec(V1, "rush", seed=11)).run()
    second = Engine(_spec(V1, "rush", seed=12)).run()
    assert first.events != second.events
    for result in (first, second):
        assert result.metric_vector["invalid_committed_overlaps"] == 0
        assert result.metric_vector["authorized_for_flight"] is False


def test_world_snapshots_cover_the_horizon() -> None:
    result = Engine(_spec(V1, "routine")).run()
    assert len(result.snapshots) == 6
    assert result.snapshots[-1].at_us == 60 * 60 * 10**6
    for snapshot in result.snapshots:
        assert len(snapshot.aircraft) == 30
        payload = json.dumps(snapshot.as_dict(), sort_keys=True)
        assert "status" in payload
        assert "pads_free" in payload


# ------------------------------------------------------------------ run-level determinism
def test_same_bundle_same_seed_same_bytes(tmp_path: Path) -> None:
    bundle = load_bundle(BASIC)
    first = run_scenario(bundle, "sha")
    second = run_scenario(load_bundle(BASIC), "sha")
    assert [json.dumps(row, sort_keys=True) for row in first.events] == [
        json.dumps(row, sort_keys=True) for row in second.events
    ]
    assert first.metrics == second.metrics
    write_run(tmp_path / "a", first)
    write_run(tmp_path / "b", second)
    digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()  # noqa: E731
    assert digest(tmp_path / "a" / "events.jsonl") == digest(tmp_path / "b" / "events.jsonl")
    assert digest(tmp_path / "a" / "metrics.json") == digest(tmp_path / "b" / "metrics.json")


def test_output_is_independent_of_python_hash_seed(tmp_path: Path) -> None:
    script = (
        "from pathlib import Path;import sys;sys.argv=['sim','run','--scenario',sys.argv[1],"
        "'--out',sys.argv[2]];from droneops_sim_runner.cli import main;main()"
    )
    produced = []
    for seed_value in ("0", "7"):
        out_dir = tmp_path / f"hash-{seed_value}"
        subprocess.run(
            [sys.executable, "-c", script, str(BASIC), str(out_dir)],
            env={
                "PYTHONHASHSEED": seed_value,
                "PATH": "/usr/bin:/bin",
                "SYSTEMROOT": str(Path(sys.executable).drive),
            },
            check=True,
            capture_output=True,
            cwd=str(REPO_ROOT),
        )
        produced.append(hashlib.sha256((out_dir / "events.jsonl").read_bytes()).hexdigest())
    assert produced[0] == produced[1]


def test_batch_runs_twenty_seeded_scenarios_deterministically(tmp_path: Path) -> None:
    runner = CliRunner()
    digests = []
    for label in ("first", "second"):
        out_dir = tmp_path / label
        result = runner.invoke(
            main,
            ["batch", "--scenario", str(BASIC), "--seeds", "20", "--out", str(out_dir)],
        )
        assert result.exit_code == 0, result.output
        assert "Simulation only. Not authorised for flight." in result.output
        assert len(list(out_dir.glob("run-*"))) == 20
        digests.append((out_dir / "batch_summary.json").read_bytes())
    assert digests[0] == digests[1]
    summary = json.loads(digests[0])
    assert len(summary["runs"]) == 20
    assert all(row["invalid_committed_overlaps"] == 0 for row in summary["runs"])
