"""Shared builders for the dispatcher tests.

Named ``dispatch_support`` rather than reusing another package's support module,
because several packages here have a ``tests/conftest.py`` and a flat-name import
would resolve to whichever was loaded first.

The runner at the bottom is the only place this suite touches ``droneops_planner``.
The dispatcher package may not depend on the planner, but a comparison *about*
routing strategies has to run real strategies, so
:class:`droneops_dispatcher.dispatch.StrategyRunner` exists precisely so the
composition happens here instead of inside the package.
"""

from __future__ import annotations

import dataclasses
import random
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml

from droneops_contracts import AircraftStatus, LinkQuality, MissionStatus, PriorityClass
from droneops_corridor_graph import CorridorGraph, load_graph
from droneops_dispatcher import (
    DispatchableMission,
    DispatchPolicy,
    FleetAircraft,
    MissionLegs,
    RepositioningLeg,
    RoundInputs,
    RunOutcome,
    SolverSettings,
)
from droneops_energy import WindSnapshot, load_profile, snapshot_from_rows
from droneops_facilities import (
    CapacityProfile,
    FacilityScheduler,
    TurnaroundDurations,
    load_capacity_profiles,
)
from droneops_planner import (
    AircraftEnvelope,
    AircraftState,
    PlanRequest,
    PolicySnapshot,
    StrategyComparator,
    default_policy,
    identical_inputs,
    identical_inputs_digest,
)

MINUTE_US = 60 * 1_000_000
SECOND_US = 1_000_000
REPO_ROOT = Path(__file__).resolve().parents[3]
BUNDLE = REPO_ROOT / "simulation" / "scenarios" / "chandigarh-synthetic-v1"
COMPETITION_DIR = REPO_ROOT / "simulation" / "scenarios" / "dispatch-competition-v1"
SAFETY_DIR = REPO_ROOT / "simulation" / "scenarios" / "dispatch-safety-v1"
PROFILE_X = ("airframe-x-sim", "illustrative-1")
FACILITY_POLICY = "sim-facility-policy-v1"


def document(name: str, directory: Path = BUNDLE) -> Mapping[str, Any]:
    return yaml.safe_load((directory / name).read_text(encoding="utf-8"))


def comparison_config() -> Mapping[str, Any]:
    return document("comparison.yaml", COMPETITION_DIR)


def safety_scenarios() -> Mapping[str, Any]:
    return document("scenarios.yaml", SAFETY_DIR)


def mission_scenario(name: str) -> Mapping[str, Any]:
    scenarios: Mapping[str, Any] = safety_scenarios()["scenarios"]
    return dict(scenarios[name])


def policy_with(**overrides: object) -> DispatchPolicy:
    """The shipped policy with named overrides, so each test states what it changed."""
    base = DispatchPolicy(config_release_sha="sha256:" + "cd" * 32)
    return dataclasses.replace(base, **overrides)


def weather(*, bundle_sha256: str = "e" * 64, retrieved_at_us: int = 0) -> WindSnapshot:
    """A calm, dated, 24-hour frozen trace: zero wind, every minute of the day."""
    return snapshot_from_rows(
        [(minute, 0, 0, 0) for minute in range(24 * 60)],
        station="chandigarh-synthetic",
        source="frozen:dispatch-safety-v1",
        bundle_sha256=bundle_sha256,
        retrieved_at_us=retrieved_at_us,
        expires_at_us=retrieved_at_us + 24 * 60 * MINUTE_US,
    )


def aircraft(
    aircraft_id: str,
    *,
    status: AircraftStatus = AircraftStatus.AVAILABLE,
    link: LinkQuality = LinkQuality.GOOD,
    charge_bp: int = 10_000,
    capacity_wh: int = 400,
    as_of_us: int = 0,
    busy_until_us: int = 0,
    home_facility_id: str = "hub-central-synthetic",
    airborne_mission_id: str | None = None,
) -> FleetAircraft:
    return FleetAircraft(
        aircraft_id=aircraft_id,
        status=status,
        link=link,
        profile_id=PROFILE_X[0],
        profile_version=PROFILE_X[1],
        capacity_wh=capacity_wh,
        charge_bp=charge_bp,
        home_facility_id=home_facility_id,
        cruise_speed_mm_per_s=15_000,
        max_payload_g=1500,
        last_observation_us=as_of_us,
        busy_until_us=busy_until_us,
        airborne_mission_id=airborne_mission_id,
    )


def legs(
    mission_id: str,
    *,
    payload_g: int = 500,
    outbound_mm: int = 6_000_000,
    handover_mm: int = 0,
    recovery_mm: int = 1_000_000,
    climb_mm: int = 90_000,
    hover_us: int = 0,
) -> MissionLegs:
    return MissionLegs(
        mission_id=mission_id,
        payload_g=payload_g,
        outbound_mm=outbound_mm,
        handover_mm=handover_mm,
        recovery_mm=recovery_mm,
        climb_mm=climb_mm,
        hover_us=hover_us,
    )


def mission(
    mission_id: str,
    *,
    priority: PriorityClass = PriorityClass.ROUTINE,
    arrived_at_us: int = 0,
    deadline_us: int = 45 * MINUTE_US,
    legs_of: MissionLegs | None = None,
    origin_facility_id: str = "hub-central-synthetic",
    turnaround_us: int = 5 * MINUTE_US,
    status: MissionStatus = MissionStatus.ELIGIBLE,
    holds_reservation: bool = False,
    aircraft_id: str | None = None,
    payload_g: int = 500,
) -> DispatchableMission:
    return DispatchableMission(
        mission_id=mission_id,
        order_id=f"ord-{mission_id}",
        priority=priority,
        status=status,
        arrived_at_us=arrived_at_us,
        deadline_us=deadline_us,
        legs=legs_of if legs_of is not None else legs(mission_id, payload_g=payload_g),
        origin_facility_id=origin_facility_id,
        turnaround_us=turnaround_us,
        holds_reservation=holds_reservation,
        aircraft_id=aircraft_id,
    )


def bundle_profiles() -> tuple[CapacityProfile, ...]:
    """The hubs the synthetic world declares - real declared capacity, not invented."""
    return load_capacity_profiles(document("facilities.yaml"), policy_version=FACILITY_POLICY)


def hub_ids(profiles: Sequence[CapacityProfile]) -> tuple[str, ...]:
    return tuple(sorted(profile.facility_id for profile in profiles))


def scheduler_for(profiles: Sequence[CapacityProfile]) -> FacilityScheduler:
    return FacilityScheduler(profiles)


def durations(*, minutes: int = 5) -> TurnaroundDurations:
    return TurnaroundDurations.from_minutes(
        loading_min=minutes, offload_min=minutes, approach_min=1, landing_min=1, departure_min=1
    )


def round_inputs(
    profiles: Sequence[CapacityProfile],
    *,
    charge_power_w: int | None = None,
    needs_recharge: bool = False,
    facility_durations: TurnaroundDurations | None = None,
) -> RoundInputs:
    """Crew capacity and alternate-site geometry, both read from the declared profiles."""
    crew = {profile.facility_id: max(1, profile.crew_slots) for profile in profiles}
    alternate_legs = {
        origin.facility_id: tuple(
            RepositioningLeg(target_facility_id=target.facility_id, leg_mm=4_000_000)
            for target in profiles
            if target.facility_id != origin.facility_id
        )
        for origin in profiles
    }
    return RoundInputs(
        weather=weather(),
        crew_capacity=crew,
        charge_power_w=charge_power_w,
        legs_to_alternates=alternate_legs,
        facility_durations=facility_durations,
        requires_recharge=needs_recharge,
    )


# --------------------------------------------------------------------- planner wiring
class _Source:
    def text(self, name: str) -> str:
        return (BUNDLE / name).read_text(encoding="utf-8")

    def document(self, name: str) -> Mapping[str, Any]:
        return yaml.safe_load(self.text(name))


_GRAPHS: list[CorridorGraph] = []


def graph() -> CorridorGraph:
    if not _GRAPHS:
        _GRAPHS.append(load_graph(_Source()))  # type: ignore[arg-type]
    return _GRAPHS[0]


def wind_steps() -> tuple[tuple[int, int, int], ...]:
    trace = document("weather.yaml")["trace"]
    return tuple(
        (int(step["minute"]), int(step["wind_ms"]), int(step["headwind_bp"])) for step in trace
    )


def planner_policy() -> PolicySnapshot:
    declared = document("policies.yaml")
    return default_policy(
        policy_version=str(declared["policy_version"]),
        config_release_sha="sha256:" + "ab" * 32,
        separation_buffer_us=int(declared["separation_buffer_us"]),
        reserve_fraction_bp=int(declared["energy_reserve_fraction_bp"]),
        wind_energy_bp_per_ms=int(declared["wind_energy_bp_per_ms"]),
        max_agl_mm=int(declared["max_agl_m"]) * 1000,
    )


def envelope(charge_wh: int, tail: str) -> AircraftEnvelope:
    """An aircraft built from the bundle's declared profile, never invented numbers."""
    profiles = document("fleet.yaml")["profiles"]
    matching = [item for item in profiles if item.get("profile_id") == PROFILE_X[0]]
    profile = dict(matching[0])
    return AircraftEnvelope(
        aircraft_id=tail,
        profile_id=str(profile["profile_id"]),
        state=AircraftState.AVAILABLE,
        link_ok=True,
        capacity_wh=int(profile["capacity_wh"]),
        charge_wh=charge_wh,
        cruise_speed_mm_per_s=int(profile["cruise_speed_mps"]) * 1000,
        max_payload_g=int(profile["max_payload_g"]),
        climb_rate_mm_per_s=2500,
        wh_per_100km=int(profile["battery_curve"][1]["wh_per_100km"]),
        home_facility_id="hub-central-synthetic",
    )


ROUTES = (
    ("hub-central-synthetic", "hub-north-synthetic"),
    ("hub-central-synthetic", "hub-ridge-east-synthetic"),
    ("node-medlab-synthetic", "hub-central-synthetic"),
    ("hub-north-synthetic", "node-quickcart-synthetic"),
    ("hub-ridge-east-synthetic", "hub-central-synthetic"),
    ("node-sparesdepot-synthetic", "hub-north-synthetic"),
)


def requests_for(seed: int, count: int, epoch_us: int) -> tuple[PlanRequest, ...]:
    """One identical demand slice per seed: same orders, states, cutoff and seed."""
    rng = random.Random(seed)
    rows: list[PlanRequest] = []
    for index in range(count):
        origin, destination = ROUTES[index % len(ROUTES)]
        rows.append(
            PlanRequest(
                tenant_id="tenant-synthetic",
                order_id=f"ord-comp-{seed}-{index:03d}",
                mission_id=f"msn-comp-{seed}-{index:03d}",
                origin_node=origin,
                destination_node=destination,
                requested_at_us=epoch_us + index * MINUTE_US,
                deadline_us=epoch_us + (index + 30) * MINUTE_US,
                cutoff_us=epoch_us + 90 * MINUTE_US,
                payload_g=500 + 100 * (index % 4),
                priority=PriorityClass.ROUTINE,
                aircraft=envelope(240 + rng.randrange(0, 160), f"air-{index % 6}-synthetic"),
                seed=seed,
                candidate_k=3,
                idempotency_key=f"idem-comp-{seed}-{index:03d}",
            )
        )
    return tuple(rows)


class PlannerStrategyRunner:
    """Runs one of the planner's three routing strategies over the shared demand."""

    def __init__(self, profiles: Sequence[CapacityProfile], *, order_count: int = 8) -> None:
        self.profiles = profiles
        self.order_count = order_count
        self.graph = graph()
        self.policy = planner_policy()
        self.wind = wind_steps()
        self._inputs: dict[int, Any] = {}

    def inputs(self, seed: int) -> Any:
        if seed not in self._inputs:
            requests = requests_for(seed, self.order_count, self.graph.metadata.epoch_us)
            self._inputs[seed] = identical_inputs(
                self.graph, self.policy, requests, self.wind, seed=seed
            )
        return self._inputs[seed]

    def run(self, strategy: str, *, seed: int) -> RunOutcome:
        inputs = self.inputs(seed)
        proposals = StrategyComparator(inputs).proposals(strategy)
        accepted = 0
        late = 0
        energy = 0
        lateness = 0
        crossings = 0
        pad_wait = 0
        for proposal in proposals:
            selected = proposal.selected
            if selected is None:
                continue
            accepted += 1
            late += 1 if selected.score.lateness_us > 0 else 0
            energy += selected.energy.consumption_wh
            lateness += selected.score.lateness_us
            crossings += selected.route.crossing_count
            pad_wait += selected.score.pad_wait_us
        unserved = len(proposals) - accepted
        profile = load_profile(*PROFILE_X)
        return RunOutcome(
            strategy=strategy,
            seed=seed,
            completed=accepted,
            late=late,
            unserved=unserved,
            rejected=unserved,
            energy_wh=energy,
            mean_lateness_us=lateness // accepted if accepted else 0,
            pad_wait_us=pad_wait,
            crew_wait_us=0,
            charger_wait_us=0,
            crossings=crossings,
            recompute_attempts=0,
            cost_units=energy * 25 + late * 500 + unserved * 10_000 + pad_wait // MINUTE_US * 120,
            demand_digest=identical_inputs_digest(
                inputs.requests, seed=seed, cutoff_us=inputs.cutoff_us
            ),
            inputs_digest=inputs.digest(),
            fleet_size=len({request.aircraft.aircraft_id for request in inputs.requests}),
            pad_capacity=sum(profile_.pad_count for profile_ in self.profiles),
            charger_capacity=sum(profile_.charger_bays for profile_ in self.profiles),
            crew_capacity=sum(profile_.crew_slots for profile_ in self.profiles),
            cutoff_us=inputs.cutoff_us,
            solver_workers=SolverSettings().num_search_workers,
            energy_profile_digest=profile.parameters_sha256,
        )


__all__ = [
    "BUNDLE",
    "FACILITY_POLICY",
    "MINUTE_US",
    "PROFILE_X",
    "REPO_ROOT",
    "ROUTES",
    "SAFETY_DIR",
    "SECOND_US",
    "PlannerStrategyRunner",
    "aircraft",
    "bundle_profiles",
    "comparison_config",
    "document",
    "durations",
    "graph",
    "hub_ids",
    "legs",
    "mission",
    "mission_scenario",
    "planner_policy",
    "policy_with",
    "round_inputs",
    "scheduler_for",
    "weather",
    "wind_steps",
]
