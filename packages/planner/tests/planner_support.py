"""Test support for the planner: real bundle data and a fully wired pipeline.

Imported by both ``conftest.py`` (which turns it into fixtures) and the test
modules; it is named uniquely because several packages in this workspace have a
``tests/conftest.py`` and importing a module literally called ``conftest`` picks up
whichever one was loaded first.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from droneops_corridor_graph import CorridorGraph, load_graph
from droneops_planner import (
    AircraftEnvelope,
    AircraftState,
    BatteryCurveEnergyModel,
    BundleFacilityScheduler,
    CandidatePlanPipeline,
    CellSweepVolumeBuilder,
    FrozenWindTrace,
    InMemoryCommitPort,
    InMemoryOccupancy,
    PipelineConfig,
    PipelinePorts,
    PlanRequest,
    PolicySnapshot,
    PriorityClass,
    default_policy,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
BUNDLE = REPO_ROOT / "simulation" / "scenarios" / "chandigarh-synthetic-v1"
MINUTE_US = 60 * 1_000_000


class DirectoryBundleSource:
    """Reads one scenario directory; the YAML decode lives here, not in the package."""

    def text(self, name: str) -> str:
        return (BUNDLE / name).read_text(encoding="utf-8")

    def document(self, name: str) -> Mapping[str, Any]:
        return yaml.safe_load(self.text(name))


def document(name: str) -> Mapping[str, Any]:
    return yaml.safe_load((BUNDLE / name).read_text(encoding="utf-8"))


def load_bundle_graph() -> CorridorGraph:
    return load_graph(DirectoryBundleSource())


def wind_trace_steps() -> tuple[tuple[int, int, int], ...]:
    trace = document("weather.yaml")["trace"]
    return tuple(
        (int(step["minute"]), int(step["wind_ms"]), int(step["headwind_bp"])) for step in trace
    )


def bundle_policy() -> PolicySnapshot:
    declared = document("policies.yaml")
    return default_policy(
        policy_version=str(declared["policy_version"]),
        config_release_sha="sha256:" + "ab" * 32,
        separation_buffer_us=int(declared["separation_buffer_us"]),
        reserve_fraction_bp=int(declared["energy_reserve_fraction_bp"]),
        wind_energy_bp_per_ms=int(declared["wind_energy_bp_per_ms"]),
        max_agl_mm=int(declared["max_agl_m"]) * 1000,
    )


def envelope(
    charge_wh: int = 380,
    state: AircraftState = AircraftState.AVAILABLE,
    *,
    link_ok: bool = True,
    profile_id: str = "airframe-x-sim",
) -> AircraftEnvelope:
    """An aircraft built from the bundle's declared profile, never invented numbers."""
    profiles = document("fleet.yaml")["profiles"]
    matching = [item for item in profiles if item.get("profile_id") == profile_id]
    if not matching:
        raise ValueError(f"bundle declares no profile {profile_id!r}")
    profile = matching[0]
    return AircraftEnvelope(
        aircraft_id="air-01-synthetic",
        profile_id=str(profile["profile_id"]),
        state=state,
        link_ok=link_ok,
        capacity_wh=int(profile["capacity_wh"]),
        charge_wh=charge_wh,
        cruise_speed_mm_per_s=int(profile["cruise_speed_mps"]) * 1000,
        max_payload_g=int(profile["max_payload_g"]),
        climb_rate_mm_per_s=2500,
        wh_per_100km=int(profile["battery_curve"][1]["wh_per_100km"]),
        home_facility_id="hub-central-synthetic",
    )


def request(
    epoch_us: int,
    *,
    origin: str = "hub-central-synthetic",
    destination: str = "hub-north-synthetic",
    order_id: str = "ord-test-001",
    mission_id: str = "msn-test-001",
    at_minute: int = 0,
    deadline_minute: int = 45,
    cutoff_minute: int = 60,
    payload_g: int = 500,
    aircraft: AircraftEnvelope | None = None,
    candidate_k: int = 4,
) -> PlanRequest:
    """One demand item in bundle minutes, converted the same way the loader does."""
    return PlanRequest(
        tenant_id="tenant-synthetic",
        order_id=order_id,
        mission_id=mission_id,
        origin_node=origin,
        destination_node=destination,
        requested_at_us=epoch_us + at_minute * MINUTE_US,
        deadline_us=epoch_us + deadline_minute * MINUTE_US,
        cutoff_us=epoch_us + cutoff_minute * MINUTE_US,
        payload_g=payload_g,
        priority=PriorityClass.ROUTINE,
        aircraft=aircraft if aircraft is not None else envelope(),
        seed=202609300,
        candidate_k=candidate_k,
        idempotency_key=f"idem-{order_id}",
    )


def build_pipeline(
    graph: CorridorGraph,
    policy: PolicySnapshot,
    wind_steps: tuple[tuple[int, int, int], ...],
    *,
    committed: tuple[tuple[str, int, int, str], ...] = (),
    strategy: str = "structured_corridor",
    max_recompute: int = 3,
    candidate_k: int = 4,
    ledger: Any | None = None,
    occupancy: InMemoryOccupancy | None = None,
    volumes: Any | None = None,
) -> tuple[CandidatePlanPipeline, InMemoryOccupancy, Any]:
    """A fully wired pipeline with an isolated ledger: one per test, never shared."""
    used_occupancy = occupancy if occupancy is not None else InMemoryOccupancy()
    for identifier, start_us, end_us, owner in committed:
        used_occupancy.preload(identifier, start_us, end_us, owner)
    used_commit = ledger if ledger is not None else InMemoryCommitPort(used_occupancy)
    ports = PipelinePorts(
        occupancy=used_occupancy,
        volumes=volumes if volumes is not None else CellSweepVolumeBuilder(graph),
        commit=used_commit,
        energy=BatteryCurveEnergyModel(),
        facilities=BundleFacilityScheduler(graph, used_occupancy),
        wind=FrozenWindTrace(wind_steps, graph.metadata.epoch_us),
    )
    pipeline = CandidatePlanPipeline(
        graph,
        policy,
        ports,
        PipelineConfig(strategy=strategy, max_recompute=max_recompute, candidate_k=candidate_k),
    )
    return pipeline, used_occupancy, used_commit


def identifiers_for(graph: CorridorGraph, edge_id: str) -> list[str]:
    """The volume-cell identifiers a lane reserves, exactly as the pipeline queries them."""
    return [f"{cell.token()}-{cell.reference.value}" for cell in graph.edge_cells_for(edge_id)]
