"""THE REAL READ PLANE - corridor-graph geometry, planner strategies, dispatcher comparison.

Unlike :mod:`droneops_api.reference` (a test double) this module answers from the packages
the Phase 2 and Phase 3 work built:

* route geometry and adjacency come from ``droneops_corridor_graph.load_graph`` over the
  frozen ``chandigarh-synthetic-v1`` bundle - real lane edges, their declared vertices and
  lengths - not from a hard-coded lane grid;
* the three routing lanes are :mod:`droneops_planner`'s own strategies, run by
  :class:`droneops_dispatcher.ScenarioComparison` over the seed list declared in
  ``simulation/scenarios/dispatch-competition-v1/comparison.yaml`` (21 seeds x 3 arms), and
  every figure the console reads is the **median and spread over those seeds**, never one
  lucky run;
* fairness is the dispatcher's, not ours: :class:`WeakenedBaselineError` arrives as a
  *failed comparison* on the wire, never as a silent verdict;
* economics follow ``docs/05`` section 5.8 (formulas and denominators) and 5.9 (cost
  components). Every monetary figure is **illustrative** and carries the chapter-7 caveat
  that the model is unvalidated and needs real quotes.

Nothing here commands an aircraft, and nothing here touches a database: this is a read
plane over frozen scenario inputs.
"""

from __future__ import annotations

import dataclasses
import hashlib
import platform as host
import random
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from pathlib import Path
from statistics import median
from typing import Any

import yaml

from droneops_api.ports import (
    ControlPlaneError,
    Principal,
    ResourceNotFoundError,
    SimulationPort,
)
from droneops_api.provenance import SourceKind, provenance_fields
from droneops_api.schemas import (
    AircraftChargeState,
    CostModelResponse,
    EconomicsMetricResponse,
    EconomicsResponse,
    ReplayDiffResponse,
    RunIdentitySchema,
    ScenarioComparisonResponse,
    SimulationRunResponse,
    StrategyLaneRow,
)
from droneops_corridor_graph import CorridorGraph, load_graph
from droneops_dispatcher import (
    MIN_COMPARISON_REPETITIONS,
    ComparisonReport,
    DispatchInputError,
    DispatchPolicy,
    InsufficientRepetitionsError,
    RunOutcome,
    ScenarioComparison,
    StrategyReport,
    WeakenedBaselineError,
)
from droneops_energy import load_profile
from droneops_facilities import CapacityProfile, load_capacity_profiles
from droneops_geom_core import geodesic_distance_mm
from droneops_planner import (
    AircraftEnvelope,
    AircraftState,
    ComparisonInputs,
    PlanRequest,
    PolicySnapshot,
    PriorityClass,
    StrategyComparator,
    default_policy,
    identical_inputs,
    identical_inputs_digest,
)

MINUTE_US = 60 * 1_000_000
MICROS_PER_KILOWATT_HOUR = 3_600_000_000
REPO_ROOT = Path(__file__).resolve().parents[4]
BUNDLE_DIR = REPO_ROOT / "simulation" / "scenarios" / "chandigarh-synthetic-v1"
COMPETITION_DIR = REPO_ROOT / "simulation" / "scenarios" / "dispatch-competition-v1"
PROFILE_X = ("airframe-x-sim", "illustrative-1")
FACILITY_POLICY = "sim-facility-policy-v1"
ENGINE_VERSION = "sim-1.0.0"
SCENARIO = "chandigarh-synthetic-v1"
ORDER_COUNT = 8
#: The priority-impact probe is a second race per seed, so it samples a declared prefix of
#: the seed list. The sample size travels with the metric's denominator; it is not hidden.
PRIORITY_IMPACT_SEEDS = 5

#: The three arms ``docs/05`` 5.6 requires, each with the run-id suffix it is registered
#: under. The ``c`` arm had no registered run before this module; it is the hybrid strategy
#: and it is a real arm with real seeded runs.
ARMS: tuple[tuple[str, str], ...] = (
    ("a", "constraint_aware_direct"),
    ("b", "structured_corridor"),
    ("c", "hybrid"),
)
STRATEGIES: tuple[str, ...] = tuple(strategy for _, strategy in ARMS)
#: The console asks for runs under both the bundle name and its own scenario name.
SCENARIO_ALIASES: tuple[str, ...] = (SCENARIO, "chandigarh-basic")

# ---------------------------------------------------------------- illustrative cost model
# docs/07 section 7.4: planning arithmetic only, to be replaced with real quotes.
FIXED_PLUS_CAPITAL_INR_PER_HUB_YEAR = 23_700_000
VARIABLE_BATTERY_INR = 150
VARIABLE_WEAR_INR = 40
VARIABLE_ENERGY_INR = 5
VARIABLE_LOSS_PROVISION_INR = 20
VARIABLE_TOTAL_INR = 215
OPERATING_DAYS_PER_YEAR = 300
ENERGY_TARIFF_PAISE_PER_KWH = 800
ROAD_BASELINE_LOW_INR = 39
ROAD_BASELINE_HIGH_INR = 67
FX_INR_PER_USD = 88
COST_COMPONENTS: tuple[str, ...] = (
    "aircraft_and_hub_depreciation",
    "batteries_and_maintenance",
    "staff",
    "energy",
    "insurance",
    "facility",
    "repositioning_and_failed_missions",
)
ILLUSTRATIVE_CAVEAT = (
    "Illustrative planning arithmetic from docs/07 section 7.4. Unvalidated: no quotes, no "
    "pilot data. Not a business number and not a validated cost."
)

CONFIG_RELEASE_SHA = "sha256:" + hashlib.sha256(b"droneops-phase-4-dispatch-plane").hexdigest()


def _document(directory: Path, name: str) -> dict[str, Any]:
    """A frozen scenario document. ``Any`` at this boundary is the YAML, not the domain."""

    loaded: dict[str, Any] = yaml.safe_load((directory / name).read_text(encoding="utf-8"))
    return loaded


def _integer(document: Mapping[str, Any], name: str) -> int:
    return int(document[name])


class _BundleSource:
    """The file access ``load_graph`` needs, pointed at the frozen bundle only."""

    def __init__(self, directory: Path = BUNDLE_DIR) -> None:
        self._directory = directory

    def text(self, name: str) -> str:
        return (self._directory / name).read_text(encoding="utf-8")

    def document(self, name: str) -> dict[str, Any]:
        return _document(self._directory, name)


_GRAPH: CorridorGraph | None = None


def corridor_graph() -> CorridorGraph:
    """The real corridor graph the planner routes on, loaded once per process."""

    global _GRAPH
    if _GRAPH is None:
        _GRAPH = load_graph(_BundleSource())
    return _GRAPH


def graph_adjacency() -> dict[str, tuple[str, ...]]:
    """Real corridor adjacency: lane neighbours and physical crossings, from the graph.

    Two edges are neighbours when they meet at a node (one ends where the other starts) or
    when the graph says their lanes cross. This is what the frozen grid approximated by
    hand; here it is derived, so a lane the bundle declares cannot be left out.
    """

    graph = corridor_graph()
    touching: dict[str, set[str]] = {edge_id: set() for edge_id in graph.edge_ids}
    by_node: dict[str, list[str]] = {}
    for edge_id in graph.edge_ids:
        edge = graph.edge(edge_id)
        by_node.setdefault(edge.from_node, []).append(edge_id)
        by_node.setdefault(edge.to_node, []).append(edge_id)
    for incident in by_node.values():
        for edge_id in incident:
            touching[edge_id].update(other for other in incident if other != edge_id)
    for edge_id in graph.edge_ids:
        touching[edge_id].update(graph.crossing_partners(edge_id))
    adjacency: dict[str, tuple[str, ...]] = {}
    for edge_id, neighbours in touching.items():
        key = graph.edge_resource_key(edge_id).identifier
        adjacency[f"corridor:{key}"] = tuple(
            sorted(graph.edge_resource_key(other).identifier for other in neighbours)
        )
    return dict(sorted(adjacency.items()))


def lane_geometry() -> tuple[dict[str, object], ...]:
    """Every real lane edge with its declared vertices and length - the corridor, not a grid."""

    graph = corridor_graph()
    rows: list[dict[str, object]] = []
    for edge_id in graph.edge_ids:
        edge = graph.edge(edge_id)
        rows.append(
            {
                "edge_id": edge.edge_id,
                "resource_key": graph.edge_resource_key(edge_id).identifier,
                "lane_group_id": edge.lane_group_id,
                "band": edge.band,
                "length_mm": edge.length_mm,
                "altitude_reference": edge.reference.value,
                "synthetic": edge.synthetic,
                "authoritative": edge.authoritative,
                "vertices": [asdict(point) for point in edge.geometry],
            }
        )
    return tuple(rows)


def facility_document() -> dict[str, Any]:
    return _document(BUNDLE_DIR, "facilities.yaml")


def capacity_profiles() -> tuple[CapacityProfile, ...]:
    return load_capacity_profiles(facility_document(), policy_version=FACILITY_POLICY)


def comparison_config() -> dict[str, Any]:
    return _document(COMPETITION_DIR, "comparison.yaml")


def seed_list() -> tuple[int, ...]:
    return tuple(int(seed) for seed in comparison_config()["seeds"])


def configured_strategies() -> tuple[str, ...]:
    return tuple(str(name) for name in comparison_config()["strategies"])


def planner_policy() -> PolicySnapshot:
    declared = _document(BUNDLE_DIR, "policies.yaml")
    return default_policy(
        policy_version=str(declared["policy_version"]),
        config_release_sha="sha256:" + "ab" * 32,
        separation_buffer_us=_integer(declared, "separation_buffer_us"),
        reserve_fraction_bp=_integer(declared, "energy_reserve_fraction_bp"),
        wind_energy_bp_per_ms=_integer(declared, "wind_energy_bp_per_ms"),
        max_agl_mm=_integer(declared, "max_agl_m") * 1000,
    )


def wind_steps() -> tuple[tuple[int, int, int], ...]:
    trace = _document(BUNDLE_DIR, "weather.yaml")["trace"]
    return tuple(
        (_integer(step, "minute"), _integer(step, "wind_ms"), _integer(step, "headwind_bp"))
        for step in trace
    )


def fleet_profile() -> dict[str, Any]:
    profiles = _document(BUNDLE_DIR, "fleet.yaml")["profiles"]
    matching = [item for item in profiles if item.get("profile_id") == PROFILE_X[0]]
    return dict(matching[0])


def envelope(charge_wh: int, tail: str) -> AircraftEnvelope:
    """An aircraft built from the bundle's declared profile, never invented numbers."""

    profile = fleet_profile()
    second = profile["battery_curve"][1]
    return AircraftEnvelope(
        aircraft_id=tail,
        profile_id=str(profile["profile_id"]),
        state=AircraftState.AVAILABLE,
        link_ok=True,
        capacity_wh=_integer(profile, "capacity_wh"),
        charge_wh=charge_wh,
        cruise_speed_mm_per_s=_integer(profile, "cruise_speed_mps") * 1000,
        max_payload_g=_integer(profile, "max_payload_g"),
        climb_rate_mm_per_s=2500,
        wh_per_100km=_integer(second, "wh_per_100km"),
        home_facility_id="hub-central-synthetic",
    )


#: Real declared hub and node ids from the corridor bundle.
LANES: tuple[tuple[str, str], ...] = (
    ("hub-central-synthetic", "hub-north-synthetic"),
    ("hub-central-synthetic", "hub-ridge-east-synthetic"),
    ("node-medlab-synthetic", "hub-central-synthetic"),
    ("hub-north-synthetic", "node-quickcart-synthetic"),
    ("hub-ridge-east-synthetic", "hub-central-synthetic"),
    ("node-sparesdepot-synthetic", "hub-north-synthetic"),
)


def requests_for(seed: int, count: int, epoch_us: int) -> tuple[PlanRequest, ...]:
    """One identical demand slice per seed: same orders, charge states, cutoff and seed."""

    rng = random.Random(seed)
    rows: list[PlanRequest] = []
    for index in range(count):
        origin, destination = LANES[index % len(LANES)]
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


def direct_distance_mm(request: PlanRequest) -> int:
    """The feasible direct distance: the geodesic between the two declared nodes."""

    graph = corridor_graph()
    origin = graph.node(request.origin_node).position
    destination = graph.node(request.destination_node).position
    return int(geodesic_distance_mm(origin, destination))


def charge_time_us(energy_wh: int, charge_power_w: int) -> int:
    """Charge duration from the mission's own energy at the hub's declared power."""

    if charge_power_w <= 0:
        return 0
    return energy_wh * MICROS_PER_KILOWATT_HOUR // (charge_power_w * 1000)


def facility_waits(
    arrivals: Sequence[tuple[str, int, int]],
    *,
    bays: int,
    crew_slots: int,
    charge_power_w: int,
    handling_us: int,
) -> tuple[int, int, list[int]]:
    """Charger wait, crew wait and per-mission waits, served first-come over declared capacity.

    ``arrivals`` is ``(mission_id, arrival_us, energy_wh)``. Bays and crew slots are the
    **declared** counts in ``facilities.yaml``; charge time is the mission's own energy over
    the hub's declared charge power; crew occupancy is the declared handling window. Ordered
    by ``(arrival_us, mission_id)`` so the answer never depends on dict or set order.
    """

    ordered = sorted(arrivals, key=lambda row: (row[1], row[0]))
    bay_count = max(1, bays)
    crew_count = max(1, crew_slots)
    bay_free = [0] * bay_count
    crew_free = [0] * crew_count
    charger_wait = 0
    crew_wait = 0
    per_mission: list[int] = []
    for index, (_mission_id, arrival_us, energy_wh) in enumerate(ordered):
        bay = index % bay_count
        wait = max(0, bay_free[bay] - arrival_us)
        charger_wait += wait
        per_mission.append(wait)
        bay_free[bay] = arrival_us + wait + charge_time_us(energy_wh, charge_power_w)
        slot = index % crew_count
        crew_at = max(0, crew_free[slot] - arrival_us)
        crew_wait += crew_at
        crew_free[slot] = arrival_us + crew_at + handling_us
    return charger_wait, crew_wait, per_mission


def p95(values: Sequence[int]) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, round(0.95 * len(ordered)) - 1))]


class ComparisonRunner:
    """Runs one planner routing strategy for one seed and keeps the measures alongside it.

    ``RunOutcome`` carries what the dispatcher compares; the side channels (planning
    latency, per-mission waits, flown versus direct distance) are what the economics of
    section 5.8 need and ``RunOutcome`` has no field for.
    """

    def __init__(self, *, order_count: int = ORDER_COUNT) -> None:
        self.order_count = order_count
        self.graph = corridor_graph()
        self.policy = planner_policy()
        self.wind = wind_steps()
        self.profiles = capacity_profiles()
        self._inputs: dict[int, ComparisonInputs] = {}
        self.plan_latency_us: dict[tuple[str, int], list[int]] = {}
        self.mission_waits_us: dict[tuple[str, int], list[int]] = {}
        self.unfinished_at_cutoff: dict[tuple[str, int], int] = {}
        self.flown_mm: dict[tuple[str, int], int] = {}
        self.direct_mm: dict[tuple[str, int], int] = {}
        self.charge_states: dict[int, list[AircraftChargeState]] = {}

    def inputs(self, seed: int) -> ComparisonInputs:
        if seed not in self._inputs:
            requests = requests_for(seed, self.order_count, self.graph.metadata.epoch_us)
            self._inputs[seed] = identical_inputs(
                self.graph, self.policy, requests, self.wind, seed=seed
            )
            self.charge_states[seed] = [
                AircraftChargeState(
                    aircraft_id=request.aircraft.aircraft_id,
                    mission_id=request.mission_id,
                    charge_wh=request.aircraft.charge_wh,
                    capacity_wh=request.aircraft.capacity_wh,
                    charge_bp=(
                        10_000 * request.aircraft.charge_wh // max(1, request.aircraft.capacity_wh)
                    ),
                    state="declared charge drawn once per seed and shared by every lane",
                    **provenance_fields(SourceKind.SYNTHETIC),
                )
                for request in requests
            ]
        return self._inputs[seed]

    def capacity(self) -> tuple[int, int, int, int, int]:
        """Declared bays, crew slots, pads, mean charge power and handling window."""

        bays = sum(profile.charger_bays for profile in self.profiles)
        crew = sum(profile.crew_slots for profile in self.profiles)
        pads = sum(profile.pad_count for profile in self.profiles)
        powers = [profile.charge_power_w for profile in self.profiles if profile.charge_power_w > 0]
        power = sum(powers) // len(powers) if powers else 0
        handling_us = max(
            (
                profile.turnaround.loading_us + profile.turnaround.handover_us
                for profile in self.profiles
            ),
            default=5 * MINUTE_US,
        )
        return bays, crew, pads, power, handling_us

    def run(self, strategy: str, *, seed: int) -> RunOutcome:
        inputs = self.inputs(seed)
        comparator = StrategyComparator(inputs)
        began = time.perf_counter_ns()
        proposals = comparator.proposals(strategy)
        elapsed_us = max(1, (time.perf_counter_ns() - began) // 1_000)
        latencies: list[int] = []
        arrivals: list[tuple[str, int, int]] = []
        accepted = 0
        late = 0
        energy = 0
        lateness = 0
        crossings = 0
        pad_wait = 0
        flown_mm = 0
        direct_mm = 0
        rejected = 0
        unfinished = 0
        for request, proposal in zip(inputs.requests, proposals, strict=True):
            selected = proposal.selected
            if selected is None:
                if proposal.trace.rejected:
                    rejected += 1
                else:
                    unfinished += 1
                continue
            accepted += 1
            late += 1 if selected.score.lateness_us > 0 else 0
            energy += selected.energy.consumption_wh
            lateness += selected.score.lateness_us
            crossings += selected.route.crossing_count
            pad_wait += selected.score.pad_wait_us
            flown_mm += selected.route.length_mm
            direct_mm += direct_distance_mm(request)
            arrivals.append(
                (request.mission_id, selected.arrival_us, selected.energy.consumption_wh)
            )

        bays, crew, _pads, power, handling_us = self.capacity()
        charger_wait, crew_wait, per_mission = facility_waits(
            arrivals,
            bays=bays,
            crew_slots=crew,
            charge_power_w=power,
            handling_us=handling_us,
        )
        unserved = rejected + unfinished
        latencies = [max(1, elapsed_us // max(1, len(inputs.requests)))]
        self.plan_latency_us[(strategy, seed)] = latencies
        self.mission_waits_us[(strategy, seed)] = per_mission
        self.unfinished_at_cutoff[(strategy, seed)] = unfinished
        self.flown_mm[(strategy, seed)] = flown_mm
        self.direct_mm[(strategy, seed)] = direct_mm

        profile = load_profile(*PROFILE_X)
        return RunOutcome(
            strategy=strategy,
            seed=seed,
            completed=accepted,
            late=late,
            unserved=unserved,
            rejected=rejected,
            energy_wh=energy,
            mean_lateness_us=lateness // accepted if accepted else 0,
            pad_wait_us=pad_wait,
            crew_wait_us=crew_wait,
            charger_wait_us=charger_wait,
            crossings=crossings,
            recompute_attempts=0,
            cost_units=energy * 25 + late * 500 + unserved * 10_000 + pad_wait // MINUTE_US * 120,
            demand_digest=identical_inputs_digest(
                inputs.requests, seed=seed, cutoff_us=inputs.cutoff_us
            ),
            inputs_digest=inputs.digest(),
            fleet_size=len({request.aircraft.aircraft_id for request in inputs.requests}),
            pad_capacity=sum(profile_.pad_count for profile_ in self.profiles),
            charger_capacity=bays,
            crew_capacity=crew,
            cutoff_us=inputs.cutoff_us,
            solver_workers=1,
            energy_profile_digest=profile.parameters_sha256,
        )


class ComparisonEngine:
    """The dispatcher's 21-seed x 3-arm race, run once and cached per process."""

    def __init__(self, runner: ComparisonRunner | None = None) -> None:
        self.runner = runner or ComparisonRunner()
        self.seeds = seed_list()
        declared = configured_strategies()
        self.strategies = tuple(name for name in STRATEGIES if name in declared) or STRATEGIES
        self._report: ComparisonReport | None = None
        self._failure = ""
        self._priority_impact: dict[str, int] = {}

    @property
    def failure(self) -> str:
        return self._failure

    def report(self) -> ComparisonReport:
        if self._report is None and not self._failure:
            if len(self.seeds) < MIN_COMPARISON_REPETITIONS:
                self._failure = (
                    f"comparison_failed:InsufficientRepetitionsError: only {len(self.seeds)} "
                    f"seeds configured, the dispatcher needs {MIN_COMPARISON_REPETITIONS}"
                )
            else:
                comparison = ScenarioComparison(
                    runner=self.runner,
                    strategies=self.strategies,
                    seeds=self.seeds,
                    policy=DispatchPolicy(config_release_sha=CONFIG_RELEASE_SHA),
                )
                try:
                    self._report = comparison.run()
                except (
                    WeakenedBaselineError,
                    InsufficientRepetitionsError,
                    DispatchInputError,
                ) as exc:
                    self._failure = f"comparison_failed:{type(exc).__name__}:{exc}"
        if self._report is None:
            raise ControlPlaneError(self._failure or "the comparison produced no report")
        return self._report

    def arm(self, strategy: str) -> StrategyReport:
        return self.report().of(strategy)

    def priority_impact_us(self, strategy: str) -> int:
        """Median shift in routine lateness when a verified-medical order joins the demand.

        Same seed, same graph, same capacity: only the first order's priority class changes.
        The denominator is the routine missions served in both runs.
        """

        if strategy in self._priority_impact:
            return self._priority_impact[strategy]
        deltas: list[int] = []
        for seed in self.seeds[:PRIORITY_IMPACT_SEEDS]:
            inputs = self.runner.inputs(seed)
            baseline = _routine_lateness(inputs, strategy)
            promoted = dataclasses.replace(
                inputs, requests=_priority_slice(inputs.requests, PriorityClass.PRIORITY)
            )
            with_priority = _routine_lateness(promoted, strategy)
            if baseline and with_priority:
                deltas.append(int(median(with_priority)) - int(median(baseline)))
        value = int(median(deltas)) if deltas else 0
        self._priority_impact[strategy] = value
        return value


def _priority_slice(
    requests: Sequence[PlanRequest], priority: PriorityClass
) -> tuple[PlanRequest, ...]:
    rows = list(requests)
    if rows:
        rows[0] = dataclasses.replace(rows[0], priority=priority)
    return tuple(rows)


def _routine_lateness(inputs: ComparisonInputs, strategy: str) -> list[int]:
    proposals = StrategyComparator(inputs).proposals(strategy)
    rows: list[int] = []
    for proposal in proposals[1:]:
        if proposal.selected is not None:
            rows.append(int(proposal.selected.score.lateness_us))
    return rows


# ------------------------------------------------------------------ economics, with formulas


def _metric(
    key: str,
    label: str,
    value: str,
    unit: str,
    formula: str,
    denominator: str,
    source: str,
    *,
    illustrative: bool,
) -> EconomicsMetricResponse:
    return EconomicsMetricResponse(
        key=key,
        label=label,
        value=value,
        unit=unit,
        formula=formula,
        denominator=denominator,
        source=source,
        illustrative=illustrative,
        **provenance_fields(SourceKind.SYNTHETIC),
    )


def _ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator > 0 else 0.0


def cost_model(deliveries_per_day_per_hub: int, energy_wh_per_delivery: int) -> CostModelResponse:
    """Section 5.9's seven components over the chapter-7 arithmetic, per completed delivery."""

    daily = max(1, deliveries_per_day_per_hub)
    annual = daily * OPERATING_DAYS_PER_YEAR
    fixed_per_drop = FIXED_PLUS_CAPITAL_INR_PER_HUB_YEAR // annual
    energy_inr = max(1, (energy_wh_per_delivery * ENERGY_TARIFF_PAISE_PER_KWH) // (100 * 1000))
    components = {
        "aircraft_and_hub_depreciation": (fixed_per_drop * 2) // 5,
        "batteries_and_maintenance": VARIABLE_BATTERY_INR + VARIABLE_WEAR_INR,
        "staff": 0,
        "energy": energy_inr,
        "insurance": fixed_per_drop // 5,
        "facility": fixed_per_drop // 2 - energy_inr if fixed_per_drop // 2 > energy_inr else 0,
        "repositioning_and_failed_missions": VARIABLE_LOSS_PROVISION_INR,
    }
    total = fixed_per_drop + VARIABLE_TOTAL_INR
    baseline = (ROAD_BASELINE_LOW_INR + ROAD_BASELINE_HIGH_INR) // 2
    return CostModelResponse(
        **provenance_fields(SourceKind.SYNTHETIC),
        illustrative=True,
        caveat=ILLUSTRATIVE_CAVEAT,
        deliveries_per_day_per_hub=daily,
        fixed_plus_capital_per_drop_inr=fixed_per_drop,
        variable_per_drop_inr=VARIABLE_TOTAL_INR,
        components={name: int(components[name]) for name in COST_COMPONENTS},
        cost_per_completed_delivery_inr=total,
        cost_per_completed_delivery_usd=round(total / FX_INR_PER_USD, 2),
        road_baseline_inr_low=ROAD_BASELINE_LOW_INR,
        road_baseline_inr_high=ROAD_BASELINE_HIGH_INR,
        cost_vs_road_baseline=round(total / baseline, 2),
        sensitivity_inr_per_drop={
            str(level): FIXED_PLUS_CAPITAL_INR_PER_HUB_YEAR // (level * OPERATING_DAYS_PER_YEAR)
            + VARIABLE_TOTAL_INR
            for level in (50, 150, 300)
        },
    )


def economics_for(engine: ComparisonEngine, strategy: str) -> EconomicsResponse:
    """The nine figures of ``docs/05`` 5.4, defined as 5.8 demands, priced as 5.9 demands."""

    report = engine.arm(strategy)
    seeds = engine.seeds
    runs = report.runs
    submitted = runs[0].completed + runs[0].unserved if runs else 0
    completed = report.median("completed")
    late = report.median("late")
    rejected = report.median("rejected")
    unserved = report.median("unserved")
    unfinished = max(0, unserved - rejected)
    energy = report.median("energy_wh")
    pad_wait = report.median("pad_wait_us")
    charger_wait = report.median("charger_wait_us")
    crew_wait = report.median("crew_wait_us")
    flown = _median_side(engine.runner.flown_mm, strategy, seeds)
    direct = _median_side(engine.runner.direct_mm, strategy, seeds)
    detour = round(_ratio(flown, direct), 4)
    latencies = sorted(
        value
        for (name, _seed), row in engine.runner.plan_latency_us.items()
        if name == strategy
        for value in row
    )
    waits = [
        value
        for (name, _seed), row in engine.runner.mission_waits_us.items()
        if name == strategy
        for value in row
    ]
    energy_per_completion = round(_ratio(energy, completed), 2)
    impact = engine.priority_impact_us(strategy)
    cost = cost_model(max(1, completed), max(1, energy // max(1, completed)))
    submitted_denominator = max(1, submitted)
    metrics = [
        _metric(
            "completed",
            "Completed orders",
            str(completed),
            "orders",
            "count of orders whose mission reached a terminal served state inside the cutoff",
            f"median over {len(seeds)} seeds",
            "dispatcher RunOutcome.completed",
            illustrative=False,
        ),
        _metric(
            "completion_rate",
            "Completion rate",
            f"{_ratio(completed, submitted) * 100:.1f}%",
            "ratio",
            "completed / all submitted orders",
            f"{submitted} submitted orders per seed",
            "docs/05 section 5.8",
            illustrative=False,
        ),
        _metric(
            "on_time_rate",
            "On-time rate",
            f"{_ratio(max(0, completed - late), submitted) * 100:.1f}%",
            "ratio",
            "completed before the requested deadline / orders carrying a deadline",
            f"{submitted_denominator} orders with deadlines per seed (every submitted order carries one)",
            "docs/05 section 5.8",
            illustrative=False,
        ),
        _metric(
            "unserved_rate",
            "Unserved rate at cutoff",
            f"{_ratio(unserved, submitted) * 100:.1f}%",
            "ratio",
            f"rejected ({rejected}) plus unfinished at cutoff ({unfinished}), without double counting",
            f"{submitted} submitted orders per seed",
            "docs/05 section 5.8",
            illustrative=False,
        ),
        _metric(
            "detour_ratio",
            "Detour ratio",
            f"{detour:.4f}",
            "ratio",
            "simulated flown distance / feasible direct distance",
            "geodesic between the declared origin and destination nodes of the same request",
            "droneops_geom_core.geodesic_distance_mm",
            illustrative=False,
        ),
        _metric(
            "energy_per_completion_wh",
            "Energy per completion",
            f"{energy_per_completion:.2f}",
            "Wh",
            f"all fleet energy ({energy} Wh), including failed and repositioning missions, / completed",
            f"{completed} completed orders",
            "docs/05 section 5.8",
            illustrative=False,
        ),
        _metric(
            "pad_wait_us",
            "Pad wait",
            str(pad_wait),
            "us",
            "summed planner pad-window wait over served missions, median over seeds",
            "missions that requested a pad inside the window",
            "droneops_planner Score.pad_wait_us",
            illustrative=False,
        ),
        _metric(
            "charger_wait_us",
            "Charger wait",
            str(charger_wait),
            "us",
            "queue wait over declared charger bays; charge time = mission Wh x 3.6e9 / (hub charge power W x 1000)",
            "missions whose aircraft needed a bay",
            "chandigarh-synthetic-v1/facilities.yaml declared capacity",
            illustrative=False,
        ),
        _metric(
            "crew_wait_us",
            "Controller and pilot wait",
            str(crew_wait),
            "us",
            "queue wait over declared crew slots at the declared loading plus handover turnaround, per mission",
            "missions needing crew at the hub",
            "chandigarh-synthetic-v1/facilities.yaml declared capacity",
            illustrative=False,
        ),
        _metric(
            "controller_minutes_per_mission",
            "Controller time per mission",
            f"{_ratio(crew_wait, completed) / MINUTE_US:.2f}",
            "min",
            "crew wait divided by completed missions",
            f"{completed} completed missions",
            "docs/05 section 5.4",
            illustrative=False,
        ),
        _metric(
            "pilot_time_minutes_per_mission",
            "Pilot time per mission",
            f"{_ratio(pad_wait + charger_wait, completed) / MINUTE_US:.2f}",
            "min",
            "aircraft-held time (pad plus charger wait) per completed mission; airborne and productive time are reported separately by utilisation",
            f"{completed} completed missions",
            "docs/05 sections 5.4 and 5.8",
            illustrative=False,
        ),
        _metric(
            "queue_age_median_us",
            "Queue age",
            str(int(median(waits)) if waits else 0),
            "us",
            "median per-mission charger wait across every seed",
            "missions that reached a charger",
            "docs/05 section 5.4",
            illustrative=False,
        ),
        _metric(
            "priority_impact_us",
            "Priority impact on ordinary missions",
            str(impact),
            "us",
            "median routine lateness when a priority order joins the same demand minus its lateness without one, per seed",
            f"routine missions served in both runs of each of the first {PRIORITY_IMPACT_SEEDS} seeds",
            "docs/05 section 5.5 step 4",
            illustrative=False,
        ),
        _metric(
            "planning_latency_median_us",
            "Planning latency (median)",
            str(int(median(latencies)) if latencies else 0),
            "us",
            "request to decision time, measured per seeded batch and divided by the plans in it; median over the batches",
            f"{len(latencies)} seeded batches on {len(seeds)} seeds",
            f"measured on python {host.python_version()} / {host.machine()}",
            illustrative=False,
        ),
        _metric(
            "planning_latency_p95_us",
            "Planning latency (p95)",
            str(p95(latencies)),
            "us",
            "the same batch measure at its 95th percentile",
            f"{len(latencies)} seeded batches on {len(seeds)} seeds",
            f"measured on python {host.python_version()} / {host.machine()}",
            illustrative=False,
        ),
        _metric(
            "cost_per_completion",
            "Modelled cost per completed delivery",
            f"{cost.cost_per_completed_delivery_inr} INR",
            "INR (illustrative)",
            "depreciation + batteries and maintenance + staff + energy + insurance + facility + repositioning and failed missions / completed deliveries",
            f"{completed} completed deliveries at {cost.deliveries_per_day_per_hub} drops per day per hub",
            "docs/07 section 7.4 illustrative model, unvalidated",
            illustrative=True,
        ),
        _metric(
            "road_baseline",
            "Comparison to baseline road cost",
            f"{cost.cost_vs_road_baseline:.2f} x road",
            "ratio (illustrative)",
            f"modelled cost per delivery / illustrative road-leg midpoint of {cost.road_baseline_inr_low}-{cost.road_baseline_inr_high} INR",
            "completed deliveries",
            "docs/07 section 7.2, unvalidated",
            illustrative=True,
        ),
    ]
    return EconomicsResponse(
        metrics=metrics,
        submitted_orders=submitted,
        completed=completed,
        late=late,
        rejected=rejected,
        unfinished_at_cutoff=unfinished,
        unserved_at_cutoff=unserved,
        deadline_orders=submitted,
        seeds=list(seeds),
        repetitions=len(seeds),
        energy_wh_median=energy,
        cost_model=cost,
        charge_states=list(engine.runner.charge_states.get(seeds[len(seeds) // 2], [])),
        illustrative=True,
        **provenance_fields(SourceKind.SYNTHETIC),
    )


def _median_side(side: Mapping[tuple[str, int], int], strategy: str, seeds: Sequence[int]) -> int:
    rows = [side[key] for key in side if key[0] == strategy]
    return int(median(rows)) if rows else 0


_ENGINE: ComparisonEngine | None = None


def engine() -> ComparisonEngine:
    """One shared engine: the seeded race is expensive and must not be re-run per request."""

    global _ENGINE
    if _ENGINE is None:
        _ENGINE = ComparisonEngine()
    return _ENGINE


def run_id_for(scenario: str, suffix: str) -> str:
    return f"{scenario}-policy-{suffix}"


def suffix_of(strategy: str) -> str:
    for suffix, name in ARMS:
        if name == strategy:
            return suffix
    return "a"


def strategy_of(run_id: str) -> str:
    """Which arm a run id names. An unknown id is an absence, never a default.

    A bare scenario id names the first lane - the constraint-aware direct baseline the
    console divides everything else by.
    """

    for suffix, strategy in ARMS:
        if run_id.endswith(f"-policy-{suffix}"):
            return strategy
    if any(run_id == alias or run_id.startswith(f"run-{alias}") for alias in SCENARIO_ALIASES):
        return ARMS[0][1]
    raise ResourceNotFoundError()


class DispatcherSimulationPort(SimulationPort):
    """``/v1/simulations/*`` and ``/v1/scenarios/*`` answered by the real dispatcher."""

    def __init__(
        self,
        comparison: ComparisonEngine | None = None,
        *,
        config_release_sha: str = CONFIG_RELEASE_SHA,
    ) -> None:
        self._engine = comparison if comparison is not None else engine()
        self._config_release_sha = config_release_sha

    @property
    def engine(self) -> ComparisonEngine:
        return self._engine

    def _identity(self, seed: int) -> RunIdentitySchema:
        graph = corridor_graph()
        return RunIdentitySchema(
            bundle_sha256=graph.bundle_sha256,
            engine_version=ENGINE_VERSION,
            scenario_sha256=graph.topology_digest(),
            policy_version=str(comparison_config().get("scenario_id", SCENARIO)),
            config_release_sha256=self._config_release_sha,
            seed=seed,
            python_version=f"{host.python_version_tuple()[0]}.{host.python_version_tuple()[1]}",
            machine_architecture=host.machine(),
        )

    def run(self, principal: Principal, run_id: str) -> SimulationRunResponse:
        del principal
        strategy = strategy_of(run_id)
        if self._engine.failure:
            raise ControlPlaneError(self._engine.failure)
        report = self._engine.arm(strategy)
        seeds = self._engine.seeds
        completed = report.median("completed")
        unserved = report.median("unserved")
        late = report.median("late")
        submitted = completed + unserved
        identity = self._identity(seeds[len(seeds) // 2])
        return SimulationRunResponse(
            run_id=run_id,
            run_identity=identity,
            submitted_orders=submitted,
            accepted=completed,
            rejected=report.median("rejected"),
            on_time_rate=_ratio(max(0, completed - late), submitted),
            snapshot_digest=identity.bundle_sha256,
            event_hash=hashlib.sha256(f"{strategy}|{seeds}".encode()).hexdigest(),
            frozen_at=None,
            strategy=strategy,
            repetitions=len(seeds),
            median_seed=seeds[len(seeds) // 2],
            economics=economics_for(self._engine, strategy),
            **provenance_fields(SourceKind.SYNTHETIC),
        )

    def replay_diff(self, principal: Principal, run_id: str) -> ReplayDiffResponse:
        """Re-run the same seed through the same runner and compare it for real."""

        del principal
        strategy = strategy_of(run_id)
        seeds = self._engine.seeds
        seed = seeds[len(seeds) // 2]
        first = self._engine.runner.run(strategy, seed=seed)
        second = self._engine.runner.run(strategy, seed=seed)
        pairs = (
            ("completed", first.completed, second.completed),
            ("rejected", first.rejected, second.rejected),
            ("unserved", first.unserved, second.unserved),
            ("energy_wh", first.energy_wh, second.energy_wh),
            ("pad_wait_us", first.pad_wait_us, second.pad_wait_us),
            ("charger_wait_us", first.charger_wait_us, second.charger_wait_us),
            ("crew_wait_us", first.crew_wait_us, second.crew_wait_us),
        )
        differing = [name for name, left, right in pairs if left != right]
        return ReplayDiffResponse(
            run_a=self._identity(seed),
            run_b=self._identity(seed),
            event_hash_equal=first.inputs_digest == second.inputs_digest,
            metrics_equal=not differing,
            differing_metrics=differing,
            replayed_against="corridor-graph bundle " + corridor_graph().bundle_sha256[:12],
            current_world_digest=corridor_graph().topology_digest(),
            **provenance_fields(SourceKind.SYNTHETIC),
        )

    def compare(
        self, principal: Principal, scenario_id: str, against: str
    ) -> ScenarioComparisonResponse:
        del principal
        strategy_a = strategy_of(scenario_id)
        strategy_b = strategy_of(against)
        seeds = self._engine.seeds
        identity = self._identity(seeds[len(seeds) // 2])
        if self._engine.failure:
            return ScenarioComparisonResponse(
                scenario_id=scenario_id,
                against=against,
                run_a=identity,
                run_b=identity,
                compared_metrics={},
                verdict=self._engine.failure,
                comparison_error=self._engine.failure,
                seeds=list(seeds),
                repetitions=len(seeds),
                fairness_checks={},
                **provenance_fields(SourceKind.SYNTHETIC),
            )
        report = self._engine.report()
        metrics: dict[str, str] = {}
        for name in ("completed", "late", "unserved", "rejected", "energy_wh", "cost_units"):
            metrics[name] = _pair(report, strategy_a, strategy_b, name)
        for name in ("pad_wait_us", "charger_wait_us", "crew_wait_us"):
            metrics[name] = _pair(report, strategy_a, strategy_b, name)
            metrics[f"median_spread_{name}"] = _spread(report, name)
        metrics["energy_per_completion_wh"] = _pair_div(report, strategy_a, strategy_b)
        metrics["on_time_rate"] = _pair_rates(report, strategy_a, strategy_b)
        cost_a = cost_model(
            max(1, report.of(strategy_a).median("completed")),
            max(1, report.of(strategy_a).median("energy_wh")),
        )
        cost_b = cost_model(
            max(1, report.of(strategy_b).median("completed")),
            max(1, report.of(strategy_b).median("energy_wh")),
        )
        metrics["cost_per_completion"] = (
            f"{cost_a.cost_per_completed_delivery_inr} vs "
            f"{cost_b.cost_per_completed_delivery_inr} INR - illustrative, unvalidated"
        )
        metrics["road_baseline_cost"] = (
            f"{cost_a.road_baseline_inr_low}-{cost_a.road_baseline_inr_high} vs "
            f"{cost_b.road_baseline_inr_low}-{cost_b.road_baseline_inr_high} INR - illustrative"
        )
        metrics["unserved_at_cutoff"] = _pair(report, strategy_a, strategy_b, "unserved")
        metrics["aircraft_charge_state"] = _charge_state_note(self._engine, seeds)
        for name in ("completed", "energy_wh", "unserved", "rejected"):
            metrics[f"median_spread_{name}"] = _spread(report, name)
        lanes = [self._lane_row(name) for name in self._engine.strategies]
        return ScenarioComparisonResponse(
            scenario_id=scenario_id,
            against=against,
            run_a=identity,
            run_b=identity,
            compared_metrics=metrics,
            verdict=_verdict(report, strategy_a, strategy_b),
            seeds=list(seeds),
            repetitions=len(seeds),
            lanes=lanes,
            fairness_checks={key: value for key, value in report.checks},
            **provenance_fields(SourceKind.SYNTHETIC),
        )

    def _lane_row(self, strategy: str) -> StrategyLaneRow:
        report = self._engine.arm(strategy)
        runs = report.runs
        energy = report.median("energy_wh")
        completed = report.median("completed")
        return StrategyLaneRow(
            **provenance_fields(SourceKind.SYNTHETIC),
            strategy=strategy,
            run_id=run_id_for(SCENARIO, suffix_of(strategy)),
            repetitions=report.repetitions,
            median_completed=completed,
            spread_completed=report.spread("completed"),
            median_energy_wh=energy,
            spread_energy_wh=report.spread("energy_wh"),
            median_unserved=report.median("unserved"),
            spread_unserved=report.spread("unserved"),
            median_pad_wait_us=report.median("pad_wait_us"),
            median_charger_wait_us=report.median("charger_wait_us"),
            median_crew_wait_us=report.median("crew_wait_us"),
            median_cost_inr=cost_model(
                max(1, completed), max(1, energy // max(1, completed))
            ).cost_per_completed_delivery_inr,
            demand_digest=runs[0].demand_digest if runs else "",
            inputs_digest=runs[0].inputs_digest if runs else "",
            economics=economics_for(self._engine, strategy),
        )


def _charge_state_note(comparison: ComparisonEngine, seeds: Sequence[int]) -> str:
    rows = comparison.runner.charge_states.get(seeds[len(seeds) // 2], [])
    if not rows:
        return "not captured"
    return (
        "; ".join(f"{row.aircraft_id} {row.charge_wh}Wh of {row.capacity_wh}Wh" for row in rows[:4])
        + f" (of {len(rows)} aircraft, drawn once per seed and shared by every lane)"
    )


def _pair(report: ComparisonReport, a: str, b: str, metric: str) -> str:
    return (
        f"{report.of(a).median(metric)} vs {report.of(b).median(metric)} "
        f"(median of {len(report.seeds)} seeds)"
    )


def _pair_rates(report: ComparisonReport, a: str, b: str) -> str:
    rows: list[str] = []
    for strategy in (a, b):
        arm = report.of(strategy)
        served = arm.median("completed")
        total = served + arm.median("unserved")
        rows.append(f"{_ratio(max(0, served - arm.median('late')), total) * 100:.1f}%")
    return f"{rows[0]} vs {rows[1]} on time (median of {len(report.seeds)} seeds)"


def _pair_div(report: ComparisonReport, a: str, b: str) -> str:
    rows: list[str] = []
    for strategy in (a, b):
        arm = report.of(strategy)
        bottom = arm.median("completed")
        rows.append(f"{arm.median('energy_wh') / bottom:.2f}" if bottom else "not computable")
    return f"{rows[0]} vs {rows[1]} Wh per completed delivery"


def _spread(report: ComparisonReport, metric: str) -> str:
    return "; ".join(
        f"{arm.strategy}: median {arm.median(metric)} spread {arm.spread(metric)} "
        f"(n={arm.repetitions})"
        for arm in report.strategies
    )


def _verdict(report: ComparisonReport, a: str, b: str) -> str:
    arm_a = report.of(a)
    arm_b = report.of(b)
    if arm_a.median("completed") == arm_b.median("completed"):
        return (
            f"tied on median completions over {len(report.seeds)} identical seeds; "
            "fairness checks passed"
        )
    winner, loser = (a, b) if arm_a.median("completed") > arm_b.median("completed") else (b, a)
    return (
        f"{winner} completes more over {len(report.seeds)} identical seeds than {loser}; "
        "fairness checks passed"
    )


__all__ = [
    "ARMS",
    "CONFIG_RELEASE_SHA",
    "ComparisonEngine",
    "ComparisonRunner",
    "DispatcherSimulationPort",
    "SCENARIO",
    "STRATEGIES",
    "capacity_profiles",
    "charge_time_us",
    "comparison_config",
    "configured_strategies",
    "corridor_graph",
    "cost_model",
    "direct_distance_mm",
    "economics_for",
    "engine",
    "facility_waits",
    "graph_adjacency",
    "lane_geometry",
    "p95",
    "requests_for",
    "run_id_for",
    "seed_list",
    "strategy_of",
]
