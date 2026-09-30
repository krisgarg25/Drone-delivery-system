"""The candidate-plan pipeline: thirteen steps, bounded recompute, decision trace.

``technical-build/11`` section 3 is implemented literally, one numbered
:class:`~droneops_planner.types.StepRecord` per step, so a reviewer can read the
trace and see what actually ran:

1. freeze scenario and reservation version;
2. admit the order and validate it against policy;
3. build the terminal model (hub pad, climb, cruise, descent, destination pad,
   return);
4. find pad and charger windows;
5. generate the candidate corridor paths;
6. compute safe time intervals against committed occupancy;
7. build swept 4D protected volumes;
8. pre-filter overlaps by cell and time window;
9. intersect precisely;
10. evaluate energy, recovery reserve and weather;
11. rank by lateness, energy, disturbance and fairness;
12. request an atomic commit;
13. append the decision record.

Steps 5 to 11 are recomputed - never re-run from step 1 - when the ledger reports a
stale reservation version, and only up to ``max_recompute`` times, each attempt
written into the trace. The result is a :class:`~droneops_planner.types.PlanProposal`
with no field that could assert the plan is safe: only the ledger commits and only
the independent checker proves.
"""

from __future__ import annotations

from dataclasses import dataclass

from droneops_corridor_graph import CorridorGraph, CostProfile
from droneops_corridor_graph.model import CorridorNode
from droneops_geom_core import Interval, lock_order, resource_key, ResourceKind

from .model_util import MICROS_PER_MINUTE, MICROS_PER_SECOND, canonical_digest, ceil_div
from .ports import (
    CommitPort,
    EnergyModel,
    FacilityScheduler,
    OccupancySource,
    SweptVolumeBuilder,
    VolumePadding,
    WindSource,
)
from .reasons import ReasonCode, Rejection
from .space_time import (
    RouteTiming,
    build_volumes,
    chain_route_timing,
    padding_of,
    prefilter_and_intersect,
)
from .strategies import StrategyRegistry
from .volumes4d import route_profile
from .types import (
    CandidatePlan,
    CommitRequest,
    CommitState,
    DecisionTrace,
    EligibleCandidate,
    EnergyEvaluation,
    LegKind,
    PadWindow,
    PlanPoint,
    PlanProposal,
    PlanRequest,
    PolicySnapshot,
    ProtectedVolume,
    RetryRecord,
    RouteShape,
    Score,
    StepRecord,
    TerminalLeg,
)

MINUTES = MICROS_PER_MINUTE


@dataclass(frozen=True)
class PipelinePorts:
    """Everything the pipeline borrows: the ledger, the geometry, the world."""

    occupancy: OccupancySource
    volumes: SweptVolumeBuilder
    commit: CommitPort
    energy: EnergyModel
    facilities: FacilityScheduler
    wind: WindSource


@dataclass(frozen=True)
class PipelineConfig:
    """Knobs that change behaviour without changing any policy value."""

    strategy: str = "structured_corridor"
    candidate_k: int = 4
    k_min: int = 3
    k_max: int = 5
    max_recompute: int = 3
    climb_mm_per_s: int = 2_500
    descent_mm_per_s: int = 3_000

    def cost_profile(self, policy: PolicySnapshot) -> CostProfile:
        return CostProfile(
            time_cost_units_per_second=policy.time_cost_units_per_second,
            band_change_units=policy.band_change_units,
            merge_penalty_units=policy.merge_penalty_units,
            crossing_penalty_units=policy.crossing_penalty_units,
        )


class CandidatePlanPipeline:
    """One graph, one policy, one set of ports: the 13-step candidate plan pipeline."""

    def __init__(
        self,
        graph: CorridorGraph,
        policy: PolicySnapshot,
        ports: PipelinePorts,
        config: PipelineConfig | None = None,
        registry: StrategyRegistry | None = None,
    ) -> None:
        self.graph = graph
        self.policy = policy
        self.ports = ports
        self.config = config or PipelineConfig()
        self.registry = registry or StrategyRegistry.default(graph)

    def plan(self, request: PlanRequest) -> PlanProposal:
        """Run the flow and return a proposal plus the trace that produced it."""
        steps: list[StepRecord] = []
        rejections: list[Rejection] = []
        version = self.ports.occupancy.reservation_version()
        steps.append(self._step_freeze(request, version))
        admission_step, admission_rejection = self._admit(request)
        steps.append(admission_step)
        if admission_rejection is not None:
            rejections.append(admission_rejection)
            return self._finish(request, steps, rejections, (), None, version, [], "rejected")
        sequence, hub_pad, pad_rejection = self._terminals_and_windows(request, steps)
        if hub_pad is None:
            rejections.append(
                pad_rejection
                if pad_rejection is not None
                else Rejection(
                    subject=request.mission_id,
                    code=ReasonCode.PAD_WINDOW_UNAVAILABLE,
                    detail=(
                        f"no pad pair free in [{request.requested_at_us}, "
                        f"{request.cutoff_us}) for {request.origin_node} and "
                        f"{request.destination_node}"
                    ),
                    graph_version=self.graph.graph_version,
                )
            )
            return self._finish(request, steps, rejections, (), None, version, [], "rejected")
        padding = self._padding()
        retries: list[RetryRecord] = []
        attempt = 0
        while True:
            attempt += 1
            candidates, rejected, ranking, evaluate_steps = self._evaluate(
                request, sequence, hub_pad, padding, version
            )
            steps.extend(evaluate_steps)
            rejections.extend(rejected)
            proposal, retry_required = self._commit_phase(
                request, candidates, ranking, steps, rejections, version
            )
            if proposal is not None:
                return proposal
            if not retry_required:
                return self._finish(
                    request,
                    steps,
                    rejections,
                    ranking,
                    None,
                    version,
                    retries,
                    "rejected",
                    candidates=candidates,
                )
            observed = self.ports.occupancy.reservation_version()
            if observed == version or attempt > self.config.max_recompute:
                retries.append(
                    RetryRecord(
                        attempt=attempt,
                        restart_from_step=5,
                        reservation_version=observed,
                        reason_code=ReasonCode.RETRY_LIMIT_EXCEEDED.value,
                        detail=(
                            f"stopped after {attempt} attempt(s) with max_recompute="
                            f"{self.config.max_recompute} at version {observed}"
                        ),
                    )
                )
                return self._finish(
                    request,
                    steps,
                    rejections,
                    ranking,
                    None,
                    version,
                    retries,
                    "rejected",
                    candidates=candidates,
                )
            retries.append(
                RetryRecord(
                    attempt=attempt,
                    restart_from_step=5,
                    reservation_version=observed,
                    reason_code=ReasonCode.COMMIT_STALE_VERSION.value,
                    detail=f"ledger moved to version {observed}; recomputing from step 5",
                )
            )
            version = observed
            steps.append(
                StepRecord(
                    step=5,
                    name="recompute",
                    outcome="restarted",
                    detail=f"attempt {attempt + 1} restarts at step 5, version {observed}",
                    counters=(("attempt", attempt + 1), ("reservation_version", observed)),
                )
            )

    def _step_freeze(self, request: PlanRequest, version: int) -> StepRecord:
        return StepRecord(
            step=1,
            name="freeze",
            outcome="frozen",
            detail=(
                f"scenario {self.graph.topology_digest()} graph {self.graph.graph_version} "
                f"reservation_version {version} cutoff {request.cutoff_us}"
            ),
            counters=(("reservation_version", version),),
        )

    def _admit(self, request: PlanRequest) -> tuple[StepRecord, Rejection | None]:
        """Step 2: admit and validate against policy numbers, never inferred ones."""
        rejection = self._admission_detail(request)
        return (
            StepRecord(
                step=2,
                name="admit",
                outcome="rejected" if rejection is not None else "admitted",
                detail=(
                    f"{rejection.code.value}: {rejection.detail}"
                    if rejection is not None
                    else (
                        f"order {request.order_id} mission {request.mission_id} "
                        f"{request.origin_node}>{request.destination_node} payload "
                        f"{request.payload_g} g on {request.aircraft.aircraft_id}"
                    )
                ),
                counters=(("payload_g", request.payload_g), ("candidate_k", request.candidate_k)),
            ),
            rejection,
        )

    def _admission_detail(self, request: PlanRequest) -> Rejection | None:
        def reject(code: ReasonCode, detail: str) -> Rejection:
            return Rejection(
                subject=request.mission_id,
                code=code,
                detail=detail,
                graph_version=self.graph.graph_version,
            )

        origin = self.graph.nodes.get(request.origin_node)
        destination = self.graph.nodes.get(request.destination_node)
        if origin is None:
            return reject(ReasonCode.ORIGIN_UNKNOWN, request.origin_node)
        if destination is None:
            return reject(ReasonCode.DESTINATION_UNKNOWN, request.destination_node)
        if request.origin_node == request.destination_node:
            return reject(ReasonCode.ORIGIN_EQUALS_DESTINATION, request.origin_node)
        if request.deadline_us <= request.requested_at_us:
            return reject(
                ReasonCode.DEADLINE_BEFORE_ACCEPTANCE,
                f"deadline {request.deadline_us} not after requested {request.requested_at_us}",
            )
        if request.cutoff_us <= request.requested_at_us:
            return reject(
                ReasonCode.HORIZON_CLOSED, f"cutoff {request.cutoff_us} closes the horizon"
            )
        if request.candidate_k < self.config.k_min:
            return reject(
                ReasonCode.CANDIDATE_LIMIT_TOO_SMALL,
                f"requested {request.candidate_k} below k_min {self.config.k_min}",
            )
        if not request.aircraft.state.assignable:
            return reject(
                ReasonCode.AIRCRAFT_INELIGIBLE,
                f"{request.aircraft.aircraft_id} state {request.aircraft.state.value}",
            )
        if not request.aircraft.link_ok:
            return reject(
                ReasonCode.AIRCRAFT_INELIGIBLE,
                f"{request.aircraft.aircraft_id} has no link: uncertainty is not clear airspace",
            )
        if request.payload_g > request.aircraft.max_payload_g:
            return reject(
                ReasonCode.PAYLOAD_EXCEEDS_ENVELOPE,
                f"{request.payload_g} g over envelope {request.aircraft.max_payload_g} g",
            )
        origin_facility = origin.facility
        destination_facility = destination.facility
        if origin_facility is None or destination_facility is None:
            missing = (
                "pickup site " + request.origin_node
                if origin_facility is None
                else "drop-off site " + request.destination_node
            )
            return reject(
                ReasonCode.PAD_WINDOW_UNAVAILABLE,
                f"{missing} declares no pad facility in bundle {self.graph.graph_version}, "
                "so nothing can be loaded or offloaded there",
            )
        if origin_facility.pads == 0 or destination_facility.pads == 0:
            silent = [
                node.node_id
                for node, attributes in ((origin, origin_facility), (destination, destination_facility))
                if attributes.pads == 0
            ]
            return reject(
                ReasonCode.PAD_WINDOW_UNAVAILABLE,
                f"{' and '.join(silent)} declare zero pads in the frozen bundle",
            )
        if request.requested_at_us < origin_facility.open_us:
            return reject(
                ReasonCode.OUTSIDE_OPERATING_WINDOW,
                f"{origin.node_id} opens at {origin_facility.open_us}",
            )
        return None

    def _terminals_and_windows(
        self, request: PlanRequest, steps: list[StepRecord]
    ) -> tuple[tuple[LegKind, ...], PadWindow | None, Rejection | None]:
        """Steps 3 and 4: the terminal model, then the ground windows around it.

        The destination pad cannot be fixed before a route exists, so step 4 holds
        the departure pad and any required charger; the arrival pad is claimed per
        candidate inside step 10, and a candidate that cannot get one is rejected
        with ``pad_window_unavailable`` rather than planned anyway.
        """
        origin = self.graph.node(request.origin_node)
        destination = self.graph.node(request.destination_node)
        origin_facility = origin.facility
        destination_facility = destination.facility
        if origin_facility is None or destination_facility is None:
            return (), None, self._no_facility_rejection(request, origin, destination)
        loading = origin_facility.loading_turnaround_us
        offload = destination_facility.offload_turnaround_us
        sequence = (
            LegKind.HUB_PAD,
            LegKind.CLIMB,
            LegKind.CRUISE,
            LegKind.DESCENT,
            LegKind.DESTINATION_PAD,
            LegKind.RETURN,
        )
        steps.append(
            StepRecord(
                step=3,
                name="terminal_model",
                outcome="built",
                detail=(
                    "hub_pad>climb>cruise>descent>destination_pad>return for "
                    f"{origin.node_id} -> {destination.node_id} -> "
                    f"{request.aircraft.home_facility_id}, offload {offload} us"
                ),
                counters=(
                    ("legs", len(sequence)),
                    ("loading_us", loading),
                    ("offload_us", offload),
                ),
            )
        )
        hub_pad = self.ports.facilities.pad_window(
            request.origin_node,
            "load",
            not_before_us=request.requested_at_us,
            duration_us=loading,
            horizon=request.horizon(),
        )
        if hub_pad is None:
            steps.append(
                StepRecord(
                    step=4,
                    name="facility_windows",
                    outcome="unavailable",
                    detail=f"no departure pad at {origin.node_id} before {request.cutoff_us}",
                )
            )
            return sequence, None, self._pad_rejection(
                request,
                f"no pad index at {origin.node_id} is free for the declared {loading} us loading "
                f"turnaround inside [{request.requested_at_us}, {request.cutoff_us})",
            )
        charge_needed = self._needs_charge(request)
        charger = (
            self.ports.facilities.charger_window(
                origin.node_id,
                not_before_us=request.requested_at_us,
                duration_us=MINUTES * 10,
                horizon=request.horizon(),
            )
            if charge_needed
            else None
        )
        if charge_needed and charger is None:
            steps.append(
                StepRecord(
                    step=4,
                    name="facility_windows",
                    outcome="unavailable",
                    detail=(
                        f"{request.aircraft.aircraft_id} needs a charge and no bay is free "
                        f"at {origin.node_id}"
                    ),
                )
            )
            return sequence, None, self._charger_rejection(
                request,
                f"{request.aircraft.aircraft_id} needs a charge and no bay is free at "
                f"{origin.node_id} inside [{request.requested_at_us}, {request.cutoff_us})",
            )
        steps.append(
            StepRecord(
                step=4,
                name="facility_windows",
                outcome="provisional",
                detail=(
                    f"departure pad {hub_pad.resource_identifier()} "
                    f"[{hub_pad.window.start}, {hub_pad.window.end})"
                    + (f", charger {charger.resource_identifier()}" if charger else "")
                ),
                counters=(("pads", 1), ("chargers", 1 if charger else 0)),
            )
        )
        return sequence, hub_pad, None

    def _pad_rejection(self, request: PlanRequest, detail: str) -> Rejection:
        """Step 4 refusal: a named ground cause, never a silent no."""
        return Rejection(
            subject=request.mission_id,
            code=ReasonCode.PAD_WINDOW_UNAVAILABLE,
            detail=detail,
            graph_version=self.graph.graph_version,
        )

    def _charger_rejection(self, request: PlanRequest, detail: str) -> Rejection:
        return Rejection(
            subject=request.mission_id,
            code=ReasonCode.CHARGER_UNAVAILABLE,
            detail=detail,
            graph_version=self.graph.graph_version,
        )

    def _no_facility_rejection(
        self, request: PlanRequest, origin: CorridorNode, destination: CorridorNode
    ) -> Rejection:
        missing = origin.node_id if origin.facility is None else destination.node_id
        return self._pad_rejection(
            request,
            f"{missing} declares no pad facility in bundle {self.graph.graph_version}",
        )

    def _needs_charge(self, request: PlanRequest) -> bool:
        usable = request.aircraft.charge_wh - ceil_div(
            request.aircraft.capacity_wh * self.policy.reserve_fraction_bp, 10_000
        )
        return usable <= 0

    def _evaluate(
        self,
        request: PlanRequest,
        sequence: tuple[LegKind, ...],
        hub_pad: PadWindow,
        padding: VolumePadding,
        version: int,
    ) -> tuple[
        tuple[CandidatePlan, ...],
        list[Rejection],
        tuple[EligibleCandidate, ...],
        list[StepRecord],
    ]:
        """Steps 5 to 11 against one frozen reservation version."""
        steps: list[StepRecord] = []
        rejections: list[Rejection] = []
        strategy = self.registry.get(self.config.strategy)
        search_graph = strategy.graph_for(self.graph)
        horizon = request.horizon()
        candidate_set = strategy.propose(
            self.graph,
            request.origin_node,
            request.destination_node,
            k=min(max(request.candidate_k, self.config.k_min), self.config.k_max),
            profile=self.config.cost_profile(self.policy),
            horizon=horizon,
        )
        steps.append(
            StepRecord(
                step=5,
                name="candidate_paths",
                outcome="generated",
                detail=(
                    f"strategy {strategy.name}: {len(candidate_set.candidates)} candidates "
                    f"from ranking {candidate_set.ranking_digest()}; "
                    f"{len(candidate_set.closed_lane_ids)} lane(s) closed for the whole window"
                ),
                counters=(("candidates", len(candidate_set.candidates)),),
            )
        )
        if not candidate_set.candidates:
            rejections.append(
                Rejection(
                    subject=f"{request.origin_node}>{request.destination_node}",
                    code=ReasonCode.NO_ROUTE_FOUND,
                    detail=(
                        f"strategy {strategy.name} found no path; closed lanes "
                        f"{', '.join(sorted(candidate_set.closed_lane_ids)) or 'none'}"
                    ),
                    graph_version=self.graph.graph_version,
                )
            )
        eligible: list[CandidatePlan] = []
        safe_detail: list[str] = []
        volume_detail: list[str] = []
        prefilter_hits = 0
        conflicts = 0
        energy_rejections = 0
        for path in candidate_set.candidates:
            route = RouteShape(
                edge_ids=path.edge_ids,
                node_ids=path.node_ids,
                length_mm=path.length_mm,
                cruise_time_us=path.cruise_time_us,
                bands=path.bands,
                merge_nodes=path.merge_nodes,
                crossing_count=path.crossing_count,
                is_direct=strategy.name == "constraint_aware_direct",
            )
            timing = chain_route_timing(
                search_graph,
                path.edge_ids,
                horizon=horizon,
                earliest_us=hub_pad.window.end,
                padding=padding,
                occupancy=self.ports.occupancy,
                separation_us=self.policy.separation_buffer_us,
            )
            if isinstance(timing, Rejection):
                rejections.append(timing)
                continue
            safe_detail.append(f"{path.path_id}: entry {timing.entry_us} release {timing.release_us}")
            volumes = build_volumes(search_graph, timing, padding=padding, builder=self.ports.volumes)
            report = prefilter_and_intersect(search_graph, volumes, self.ports.occupancy)
            prefilter_hits += len(report.prefilter_hits)
            if not report.is_clear:
                conflicts += len(report.conflicts)
                rejections.append(
                    Rejection(
                        subject=path.path_id,
                        code=ReasonCode.VOLUME_CONFLICT,
                        detail="; ".join(
                            f"{identifier} held by {owner} over {window}"
                            for identifier, owner, window in report.conflicts
                        ),
                        graph_version=self.graph.graph_version,
                        evidence=tuple(sorted({owner for _, owner, _ in report.conflicts})),
                    )
                )
                continue
            volume_detail.append(f"{path.path_id}: {len(volumes)} protected volumes")
            evaluated = self._evaluate_candidate(search_graph, request, route, timing, volumes, sequence, hub_pad)
            if isinstance(evaluated, Rejection):
                rejections.append(evaluated)
                if evaluated.code in (
                    ReasonCode.ENERGY_BELOW_RESERVE,
                    ReasonCode.WEATHER_EXCEEDS_LANE_CAP,
                ):
                    energy_rejections += 1
                continue
            eligible.append(evaluated)
        steps.extend(
            [
                StepRecord(
                    step=6,
                    name="safe_intervals",
                    outcome="searched",
                    detail=" | ".join(safe_detail) or "no candidate reached step 6",
                    counters=(("traversed", len(safe_detail)),),
                ),
                StepRecord(
                    step=7,
                    name="protected_volumes",
                    outcome="swept",
                    detail=" | ".join(volume_detail) or "no candidate reached step 7",
                ),
                StepRecord(
                    step=8,
                    name="overlap_prefilter",
                    outcome="screened",
                    detail=f"{prefilter_hits} cell-and-window hits to intersect precisely",
                    counters=(("prefilter_hits", prefilter_hits),),
                ),
                StepRecord(
                    step=9,
                    name="precise_intersection",
                    outcome="intersected",
                    detail=f"{conflicts} exact overlap(s) against committed occupancy",
                    counters=(("conflicts", conflicts),),
                ),
                StepRecord(
                    step=10,
                    name="energy_recovery_weather",
                    outcome="evaluated",
                    detail=(
                        f"{len(eligible)} candidate(s) clear of reserve; {energy_rejections} "
                        f"rejected on energy or wind"
                    ),
                    counters=(
                        ("eligible", len(eligible)),
                        ("energy_or_weather", energy_rejections),
                    ),
                ),
            ]
        )
        ranked, rows = self._rank(eligible, steps)
        return tuple(ranked), rejections, rows, steps

    def _evaluate_candidate(
        self,
        search_graph: CorridorGraph,
        request: PlanRequest,
        route: RouteShape,
        timing: RouteTiming,
        volumes: tuple[ProtectedVolume, ...],
        sequence: tuple[LegKind, ...],
        hub_pad: PadWindow,
    ) -> CandidatePlan | Rejection:
        """Step 10 plus the pad and deadline gates, for one candidate."""

        def reject(code: ReasonCode, detail: str, evidence: tuple[str, ...] = ()) -> Rejection:
            return Rejection(
                subject=route.canonical(),
                code=code,
                detail=detail,
                graph_version=search_graph.graph_version,
                evidence=evidence,
            )

        window = Interval.of(hub_pad.window.start, timing.release_us)
        wind_mm_per_s = self.ports.wind.worst_wind_mm_per_s(window)
        for edge_id in route.edge_ids:
            if edge_id not in search_graph.edges:
                continue
            cap = search_graph.edge(edge_id).rule.max_wind_mm_per_s
            if wind_mm_per_s > cap:
                return reject(
                    ReasonCode.WEATHER_EXCEEDS_LANE_CAP,
                    f"lane {edge_id} declares {cap} mm/s, the frozen trace shows "
                    f"{wind_mm_per_s} mm/s over [{window.start}, {window.end})",
                    (f"wind_mm_per_s={wind_mm_per_s}", f"cap_mm_per_s={cap}"),
                )
        if wind_mm_per_s > self.policy.max_wind_mm_per_s:
            return reject(
                ReasonCode.WEATHER_EXCEEDS_LANE_CAP,
                f"world wind cap {self.policy.max_wind_mm_per_s} mm/s exceeded by "
                f"{wind_mm_per_s} mm/s",
            )
        climb_mm = self._climb_height_mm(search_graph, route)
        energy: EnergyEvaluation = self.ports.energy.evaluate(
            route,
            request.aircraft,
            wind_mm_per_s=wind_mm_per_s,
            headwind_bp=self.ports.wind.headwind_bp(window),
            payload_g=request.payload_g,
            reserve_fraction_bp=self.policy.reserve_fraction_bp,
            recovery_length_mm=self._recovery_length_mm(search_graph, route),
            climb_mm=climb_mm,
            wind_energy_bp_per_ms=self.policy.wind_energy_bp_per_ms,
        )
        if energy.margin_wh < 0:
            return reject(
                ReasonCode.ENERGY_BELOW_RESERVE,
                f"consumption {energy.consumption_wh} Wh + recovery {energy.recovery_wh} Wh + "
                f"reserve {energy.reserve_required_wh} Wh against {energy.available_wh} Wh "
                f"available on {request.aircraft.aircraft_id}",
            )
        destination_facility = search_graph.node(request.destination_node).facility
        if destination_facility is None:
            return reject(
                ReasonCode.PAD_WINDOW_UNAVAILABLE,
                f"destination {request.destination_node} declares no pad facility in graph "
                f"{search_graph.graph_version}",
            )
        offload_duration = destination_facility.offload_turnaround_us
        dest_pad = self.ports.facilities.pad_window(
            request.destination_node,
            "offload",
            not_before_us=timing.exit_us,
            duration_us=offload_duration,
            horizon=request.horizon(),
        )
        if dest_pad is None:
            return reject(
                ReasonCode.PAD_WINDOW_UNAVAILABLE,
                f"no destination pad free in [{timing.exit_us}, {request.cutoff_us}) for "
                f"{offload_duration} us",
            )
        arrival_us = timing.exit_us + offload_duration
        if arrival_us > request.deadline_us:
            return reject(
                ReasonCode.DEADLINE_MISSED,
                f"arrival {arrival_us} past deadline {request.deadline_us}",
                (f"lateness_us={arrival_us - request.deadline_us}",),
            )
        if not self.ports.facilities.crew_covers(request.origin_node, hub_pad.window):
            return reject(ReasonCode.CREW_UNAVAILABLE, f"no crew shift covers {hub_pad.window}")
        resources = self._resource_windows(search_graph, route, timing, volumes, hub_pad, dest_pad)
        profile = route_profile(
            search_graph,
            route.edge_ids,
            timing.timings,
            plan_version=self.policy.policy_version,
            source=self.graph.metadata.source,
        )
        return CandidatePlan(
            candidate_id=f"cand-{canonical_digest(route.canonical())[7:23]}",
            strategy=self.config.strategy,
            route=route,
            entry_us=timing.entry_us,
            exit_us=timing.exit_us,
            arrival_us=arrival_us,
            release_us=timing.release_us,
            legs=self._terminal_legs(search_graph, request, route, timing, hub_pad, dest_pad, sequence),
            pad_windows=(hub_pad, dest_pad),
            volumes=volumes,
            points=self._plan_points(search_graph, request, route, timing, hub_pad, dest_pad),
            energy=energy,
            score=self._score(request, route, energy, arrival_us, hub_pad),
            wind_mm_per_s=wind_mm_per_s,
            resource_windows=resources,
            lock_order=self._locks(resources),
            profile_points=profile.points,
        )

    def _rank(
        self, candidates: list[CandidatePlan], steps: list[StepRecord]
    ) -> tuple[list[CandidatePlan], tuple[EligibleCandidate, ...]]:
        """Step 11: rank, keeping the four numbers that produced the order visible."""
        ordered = sorted(
            candidates,
            key=lambda item: (item.score.total_units, item.route.canonical(), item.candidate_id),
        )
        rows = tuple(
            EligibleCandidate(
                candidate_id=item.candidate_id,
                strategy=item.strategy,
                route_digest=item.route.digest(),
                lateness_us=item.score.lateness_us,
                energy_margin_wh=item.energy.margin_wh,
                disturbance_units=item.score.disturbance_units,
                fairness_units=item.score.fairness_units,
                total_units=item.score.total_units,
            )
            for item in ordered
        )
        steps.append(
            StepRecord(
                step=11,
                name="ranking",
                outcome="ranked" if ordered else "no_eligible_candidate",
                detail=(
                    " > ".join(
                        f"{item.candidate_id}=total {item.score.total_units} "
                        f"(late {item.score.lateness_us} us, energy {item.energy.consumption_wh} Wh, "
                        f"disturb {item.score.disturbance_units}, fair {item.score.fairness_units})"
                        for item in ordered
                    )
                    or "every candidate was rejected before ranking"
                ),
                counters=(("eligible", len(ordered)),),
            )
        )
        return ordered, rows

    def _commit_phase(
        self,
        request: PlanRequest,
        candidates: tuple[CandidatePlan, ...],
        ranking: tuple[EligibleCandidate, ...],
        steps: list[StepRecord],
        rejections: list[Rejection],
        version: int,
    ) -> tuple[PlanProposal | None, bool]:
        """Steps 12 and 13: ask the ledger for one atomic commit, then write the record."""
        if not candidates:
            steps.append(
                StepRecord(
                    step=12,
                    name="commit",
                    outcome="not_requested",
                    detail="no eligible candidate to commit",
                )
            )
            return None, False
        for candidate in candidates:
            commit_request = CommitRequest(
                mission_id=request.mission_id,
                tenant_id=request.tenant_id,
                candidate_id=candidate.candidate_id,
                policy_version=self.policy.policy_version,
                config_release_sha=self.policy.config_release_sha,
                graph_version=self.graph.graph_version,
                reservation_version=version,
                lock_order=candidate.lock_order,
                resources=candidate.resource_windows,
            )
            outcome = self.ports.commit.try_commit(commit_request)
            steps.append(
                StepRecord(
                    step=12,
                    name="commit",
                    outcome=outcome.state.value,
                    detail=f"{candidate.candidate_id}: {outcome.detail} (planned at version {version})",
                    counters=(("resources", len(candidate.resource_windows)),),
                )
            )
            if outcome.committed:
                steps.append(
                    StepRecord(
                        step=13,
                        name="decision_record",
                        outcome="appended",
                        detail=(
                            f"selected {candidate.candidate_id}; config release "
                            f"{self.policy.config_release_sha}; policy "
                            f"{self.policy.policy_version}"
                        ),
                    )
                )
                return (
                    self._finish(
                        request,
                        steps,
                        rejections,
                        ranking,
                        candidate,
                        version,
                        [],
                        "proposed",
                        commit_request=commit_request,
                        candidates=candidates,
                    ),
                    False,
                )
            rejections.append(
                Rejection(
                    subject=candidate.candidate_id,
                    code=(
                        ReasonCode.COMMIT_STALE_VERSION
                        if outcome.state is CommitState.REJECTED_STALE_VERSION
                        else ReasonCode.COMMIT_CONFLICT
                    ),
                    detail=outcome.detail,
                    graph_version=self.graph.graph_version,
                )
            )
            if outcome.state is CommitState.REJECTED_STALE_VERSION:
                return None, True
        return None, False

    def _finish(
        self,
        request: PlanRequest,
        steps: list[StepRecord],
        rejections: list[Rejection],
        ranking: tuple[EligibleCandidate, ...],
        selected: CandidatePlan | None,
        version: int,
        retries: list[RetryRecord],
        outcome: str,
        *,
        commit_request: CommitRequest | None = None,
        candidates: tuple[CandidatePlan, ...] = (),
    ) -> PlanProposal:
        trace = DecisionTrace(
            mission_id=request.mission_id,
            scenario_digest=self.graph.topology_digest(),
            graph_version=self.graph.graph_version,
            policy_version=self.policy.policy_version,
            config_release_sha=self.policy.config_release_sha,
            reservation_version=version,
            steps=tuple(steps),
            eligible=ranking,
            rejected=tuple(rejection.as_row() for rejection in rejections),
            ranking=tuple(row.as_row() + (index,) for index, row in enumerate(ranking)),
            selected_candidate_id=None if selected is None else selected.candidate_id,
            retries=tuple(retries),
            outcome=outcome,
        )
        return PlanProposal(
            proposal_id=f"proposal-{trace.digest()[7:23]}",
            mission_id=request.mission_id,
            selected=selected,
            candidates=candidates,
            commit_request=commit_request,
            trace=trace,
            plan_points=() if selected is None else selected.points,
            protected_volumes=() if selected is None else selected.volumes,
            resources=() if selected is None else selected.resource_windows,
        )

    def _padding(self) -> VolumePadding:
        return padding_of(
            horizontal_mm=self.policy.horizontal_margin_mm,
            vertical_mm=self.policy.vertical_margin_mm,
            uncertainty_mm=self.policy.position_uncertainty_mm,
            timing_us=self.policy.timing_uncertainty_us,
            transition_us=self.policy.transition_buffer_us,
            separation_us=self.policy.separation_buffer_us,
        )

    def _score(
        self,
        request: PlanRequest,
        route: RouteShape,
        energy: EnergyEvaluation,
        arrival_us: int,
        hub_pad: PadWindow,
    ) -> Score:
        """Lateness, energy, disturbance and fairness, each kept as its own integer."""
        lateness_us = max(0, arrival_us - request.deadline_us)
        disturbance = (
            route.crossing_count * self.policy.crossing_penalty_units
            + len(route.merge_nodes) * self.policy.merge_penalty_units
        )
        fairness = (
            sum(self.ports.occupancy.lane_usage_count(edge_id) for edge_id in route.edge_ids)
            * self.policy.fairness_weight_units
        )
        pad_wait_us = hub_pad.window.start - request.requested_at_us
        total = (
            ceil_div(lateness_us, MINUTES) * self.policy.lateness_weight_units_per_minute
            + energy.consumption_wh * self.policy.energy_weight_units_per_wh
            + disturbance
            + fairness
            + ceil_div(pad_wait_us, MICROS_PER_SECOND) * self.policy.pad_queue_units
        )
        return Score(
            lateness_us=lateness_us,
            energy_wh=energy.consumption_wh,
            disturbance_units=disturbance,
            fairness_units=fairness,
            pad_wait_us=pad_wait_us,
            total_units=total,
        )

    def _terminal_legs(
        self,
        graph: CorridorGraph,
        request: PlanRequest,
        route: RouteShape,
        timing: RouteTiming,
        hub_pad: PadWindow,
        dest_pad: PadWindow,
        sequence: tuple[LegKind, ...],
    ) -> tuple[TerminalLeg, ...]:
        """The six phases with the windows this candidate actually needs."""
        del sequence
        climb_us = ceil_div(self._climb_height_mm(graph, route), self.config.climb_mm_per_s)
        descent_us = ceil_div(self._climb_height_mm(graph, route), self.config.descent_mm_per_s)
        cruise_start = hub_pad.window.end + climb_us
        first_band = route.bands[0] if route.bands else ""
        last_band = route.bands[-1] if route.bands else ""
        return (
            TerminalLeg(LegKind.HUB_PAD, request.origin_node, hub_pad.window, 0, "", first_band),
            TerminalLeg(
                LegKind.CLIMB,
                request.origin_node,
                Interval.of(hub_pad.window.end, cruise_start),
                0,
                "",
                first_band,
            ),
            TerminalLeg(
                LegKind.CRUISE,
                f"{route.node_ids[0]}>{route.node_ids[-1]}",
                Interval.of(cruise_start, cruise_start + route.cruise_time_us),
                route.length_mm,
                first_band,
                last_band,
            ),
            TerminalLeg(
                LegKind.DESCENT,
                request.destination_node,
                Interval.of(timing.exit_us - descent_us, timing.exit_us),
                0,
                last_band,
                "",
            ),
            TerminalLeg(LegKind.DESTINATION_PAD, request.destination_node, dest_pad.window, 0, last_band, ""),
            TerminalLeg(
                LegKind.RETURN,
                request.aircraft.home_facility_id,
                Interval.of(dest_pad.window.end, dest_pad.window.end + climb_us + descent_us),
                self._recovery_length_mm(graph, route),
                last_band,
                "",
            ),
        )

    def _plan_points(
        self,
        graph: CorridorGraph,
        request: PlanRequest,
        route: RouteShape,
        timing: RouteTiming,
        hub_pad: PadWindow,
        dest_pad: PadWindow,
    ) -> tuple[PlanPoint, ...]:
        """4D points a checker can re-sweep on its own: the payload the proof is built on."""
        points = [PlanPoint(hub_pad.window.start, request.origin_node, "", "", 0)]
        cumulative = 0
        for record in timing.timings:
            edge = graph.edge(record.edge_id)
            points.append(
                PlanPoint(
                    record.entry_us,
                    edge.from_node,
                    edge.edge_id,
                    edge.band,
                    cumulative,
                )
            )
            cumulative += edge.length_mm
            points.append(
                PlanPoint(record.exit_us, edge.to_node, edge.edge_id, edge.band, cumulative)
            )
        points.append(
            PlanPoint(dest_pad.window.end, request.destination_node, "", route.bands[-1] if route.bands else "", cumulative)
        )
        return tuple(points)

    def _resource_windows(
        self,
        graph: CorridorGraph,
        route: RouteShape,
        timing: RouteTiming,
        volumes: tuple[ProtectedVolume, ...],
        hub_pad: PadWindow,
        dest_pad: PadWindow,
    ) -> tuple[tuple[str, int, int], ...]:
        """Every resource this plan claims: lane cells, lanes and both pads."""
        del route
        reference = graph.metadata.altitude_reference.value
        rows: set[tuple[str, int, int]] = set()
        for volume in volumes:
            for token in volume.cells:
                rows.add((f"{token}-{reference}", volume.window.start, volume.window.end))
        for record in timing.timings:
            rows.add(
                (
                    graph.edge_resource_key(record.edge_id).identifier,
                    record.entry_us,
                    record.release_us,
                )
            )
        for pad in (hub_pad, dest_pad):
            rows.add((pad.resource_identifier(), pad.window.start, pad.window.end))
        return tuple(sorted(rows))

    def _locks(self, resources: tuple[tuple[str, int, int], ...]) -> tuple[str, ...]:
        """Canonical acquisition order, so two plans claiming the same cells cannot deadlock.

        Lanes keep the bare ``corridor:`` identity, which carries no window: that is
        the geom-core rule, and the cells beside it are the time-bounded claim.
        """
        keys = []
        for identifier, start_us, end_us in resources:
            if identifier.startswith("lane-"):
                keys.append(resource_key(ResourceKind.CORRIDOR, identifier))
            elif identifier.startswith("pad-"):
                keys.append(
                    resource_key(ResourceKind.PAD, identifier, Interval.of(start_us, end_us))
                )
            elif identifier.startswith("charger-"):
                keys.append(
                    resource_key(ResourceKind.CHARGER_BAY, identifier, Interval.of(start_us, end_us))
                )
            else:
                keys.append(
                    resource_key(ResourceKind.VOLUME_CELL, identifier, Interval.of(start_us, end_us))
                )
        return lock_order(keys)

    def _climb_height_mm(self, graph: CorridorGraph, route: RouteShape) -> int:
        bands = [graph.band_rule(band) for band in route.bands if band in graph.bands]
        if not bands:
            return 0
        return max(rule.ceiling_mm for rule in bands) - min(rule.floor_mm for rule in bands)

    def _recovery_length_mm(self, graph: CorridorGraph, route: RouteShape) -> int:
        """Declared length of the longest lane on the route, as the recovery allowance."""
        return max(
            (
                graph.edge(edge_id).length_mm
                for edge_id in route.edge_ids
                if edge_id in graph.edges
            ),
            default=0,
        )


def default_policy(
    *,
    policy_version: str,
    config_release_sha: str,
    horizontal_margin_mm: int = 15_000,
    vertical_margin_mm: int = 10_000,
    position_uncertainty_mm: int = 5_000,
    timing_uncertainty_us: int = 5 * MICROS_PER_SECOND,
    transition_buffer_us: int = 10 * MICROS_PER_SECOND,
    separation_buffer_us: int = 120 * MICROS_PER_SECOND,
    max_agl_mm: int = 90_000,
    max_wind_mm_per_s: int = 11_000,
    wind_energy_bp_per_ms: int = 250,
    reserve_fraction_bp: int = 2_500,
) -> PolicySnapshot:
    """A policy built from the values a release config declares, never invented here."""
    return PolicySnapshot(
        policy_version=policy_version,
        config_release_sha=config_release_sha,
        horizontal_margin_mm=horizontal_margin_mm,
        vertical_margin_mm=vertical_margin_mm,
        position_uncertainty_mm=position_uncertainty_mm,
        timing_uncertainty_us=timing_uncertainty_us,
        transition_buffer_us=transition_buffer_us,
        separation_buffer_us=separation_buffer_us,
        max_agl_mm=max_agl_mm,
        max_wind_mm_per_s=max_wind_mm_per_s,
        wind_energy_bp_per_ms=wind_energy_bp_per_ms,
        reserve_fraction_bp=reserve_fraction_bp,
        crossing_penalty_units=25,
        merge_penalty_units=10,
        band_change_units=200,
        time_cost_units_per_second=0,
        fairness_weight_units=5,
        lateness_weight_units_per_minute=100,
        energy_weight_units_per_wh=2,
        pad_queue_units=1,
    )


__all__ = [
    "CandidatePlanPipeline",
    "PipelineConfig",
    "PipelinePorts",
    "default_policy",
]
