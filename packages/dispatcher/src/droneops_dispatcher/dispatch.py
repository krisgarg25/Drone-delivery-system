"""The dispatch round, and the seeded scenario comparison.

A round is the Phase 3 pipeline in one place, in the order the convention specifies
(``conventions.md`` section 5, steps 3 to 6): rank *eligible* aircraft, screen them
on the complete-mission energy margin, ask the facility scheduler for a window,
price repositioning when a site is full, solve the assignment with one worker and a
fixed seed, and write a record for every mission - including the ones that did not
fly, because a rejection with no reason is a Phase 4 bug waiting to happen.

The dispatcher *proposes*. It calls ``FacilityScheduler.propose`` and no commit
method, it writes to an append-only log and not to a ledger, and every value it
returns is something an operator or the API transaction may take forward. There is
no path from here to an aircraft: *Dispatcher cannot reach a vehicle link* is kept
honest by ``.importlinter``.

Scenario comparison
    :class:`ScenarioComparison` is the "direct vs lane-based vs hybrid" harness. It
    consumes a :class:`StrategyRunner` rather than importing the planner, because
    the dispatcher owns the *fairness of the experiment* - the repetition count and
    the guard that fails loudly if any arm saw weaker inputs - while the composition
    layer owns which strategies exist. ``docs/05-chandigarh-demo.md`` section 5.6 is
    the rule this module exists to enforce: a baseline that was quietly given fewer
    aircraft, less pad capacity or a tighter cutoff is not conservative, it is
    misleading. :meth:`ScenarioComparison.run` refuses such a comparison.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, Protocol

from droneops_contracts import MissionStatus, PriorityClass
from droneops_energy import WindSnapshot, load_profile
from droneops_facilities import (
    CapacityProfile,
    DecisionState,
    FacilityScheduler,
    TurnaroundDurations,
    TurnaroundRequest,
    WaitReason,
    alternate_site_quotes,
)

from .eligibility import FleetAircraft, candidate_query
from .errors import DispatchInputError, InsufficientRepetitionsError, WeakenedBaselineError
from .policy import DispatchPolicy, digest_of
from .queue import DispatchQueue, FairnessRank, QueuedMission
from .ranking import (
    AssignmentScore,
    FacilityWaits,
    MissionLegs,
    RepositioningCost,
    RepositioningLeg,
    best_repositioning,
    price_repositioning,
    rank_scores,
    score_candidate,
    screen_fleet,
)
from .rejection import (
    AppendOnlyDecisionLog,
    DecisionOutcome,
    DispatchDecision,
    ReasonCode,
    RejectionExplanation,
    explain,
    explain_exclusion,
    explain_queue,
    explain_screen,
)
from .solver import AssignmentRequest, AssignmentSolution, solve_assignment

MIN_COMPARISON_REPETITIONS: Final[int] = 10
"""Seeded repetitions per strategy. Below this, a median is an anecdote."""

ONE_WORKER: Final[int] = 1

#: Shared-input fields every arm of a comparison must agree on, per seed.
GUARDED_INPUT_FIELDS: Final[tuple[str, ...]] = (
    "demand_digest",
    "inputs_digest",
    "fleet_size",
    "pad_capacity",
    "charger_capacity",
    "crew_capacity",
    "cutoff_us",
    "energy_profile_digest",
)

RANK_METRICS: Final[tuple[str, ...]] = (
    "completed",
    "late",
    "unserved",
    "energy_wh",
    "mean_lateness_us",
    "pad_wait_us",
    "crossings",
    "cost_units",
)


@dataclass(frozen=True)
class DispatchableMission:
    """A mission and the shape the energy kernel and the hub pads both need."""

    mission_id: str
    order_id: str
    priority: PriorityClass
    status: MissionStatus
    arrived_at_us: int
    deadline_us: int
    legs: MissionLegs
    origin_facility_id: str
    turnaround_us: int
    holds_reservation: bool = False
    aircraft_id: str | None = None

    def __post_init__(self) -> None:
        if self.turnaround_us <= 0:
            raise DispatchInputError(f"{self.mission_id}: declare a hub turn duration")
        if self.legs.mission_id != self.mission_id:
            raise DispatchInputError(
                f"{self.mission_id}: legs carry a different mission id {self.legs.mission_id}"
            )

    def to_queued(self) -> QueuedMission:
        return QueuedMission(
            mission_id=self.mission_id,
            order_id=self.order_id,
            priority=self.priority,
            arrived_at_us=self.arrived_at_us,
            deadline_us=self.deadline_us,
            payload_g=self.legs.payload_g,
            status=self.status,
            holds_reservation=self.holds_reservation,
            aircraft_id=self.aircraft_id,
        )


@dataclass(frozen=True)
class MissionOutcome:
    """What one mission got in one round, plus the record that says so."""

    mission_id: str
    outcome: DecisionOutcome
    aircraft_id: str | None
    decision: DispatchDecision
    start_us: int | None
    units: int | None
    waits: FacilityWaits
    repositioning: RepositioningCost | None

    def as_row(self) -> tuple[str, str, str]:
        return (self.mission_id, self.outcome.value, self.aircraft_id or "-")


@dataclass(frozen=True)
class DispatchRound:
    """The round's whole answer: outcomes, metrics, and the fairness events."""

    round_id: str
    as_of_us: int
    outcomes: tuple[MissionOutcome, ...]
    solution: AssignmentSolution
    starvation: tuple[FairnessRank, ...]
    order_after: tuple[str, ...]
    metrics_rows: tuple[tuple[str, object], ...]

    def of(self, outcome: DecisionOutcome) -> tuple[MissionOutcome, ...]:
        return tuple(row for row in self.outcomes if row.outcome is outcome)

    def counts(self) -> tuple[tuple[str, int], ...]:
        return (
            ("submitted", len(self.outcomes)),
            ("assigned", len(self.of(DecisionOutcome.ASSIGNED))),
            ("queued", len(self.of(DecisionOutcome.QUEUED))),
            ("rejected", len(self.of(DecisionOutcome.REJECTED))),
            ("starvation_events", len(self.starvation)),
        )

    def metric(self, name: str) -> object:
        for key, value in self.metrics_rows:
            if key == name:
                return value
        return None

    def digest(self) -> str:
        return digest_of(
            [
                ("round_id", self.round_id),
                ("as_of_us", self.as_of_us),
                (
                    "outcome",
                    ";".join(
                        f"{row.mission_id}:{row.outcome.value}:{row.aircraft_id}"
                        for row in self.outcomes
                    ),
                ),
                ("solution", self.solution.digest()),
            ]
        )


@dataclass(frozen=True)
class RoundInputs:
    """Everything a round reads from outside itself."""

    weather: WindSnapshot
    crew_capacity: Mapping[str, int]
    charge_power_w: int | None = None
    legs_to_alternates: Mapping[str, tuple[RepositioningLeg, ...]] = field(default_factory=dict)
    facility_durations: TurnaroundDurations | None = None
    requires_recharge: bool = False


class Dispatcher:
    """Aircraft assignment for one fleet view, one facility scheduler, one policy."""

    def __init__(
        self,
        *,
        policy: DispatchPolicy,
        fleet: Sequence[FleetAircraft],
        scheduler: FacilityScheduler,
        profiles: Sequence[CapacityProfile],
        log: AppendOnlyDecisionLog | None = None,
        facility_policy_version: str = "sim-facility-policy-v1",
    ) -> None:
        self.policy = policy
        self.fleet = tuple(sorted(fleet, key=lambda row: row.aircraft_id))
        self.scheduler = scheduler
        self.log = log if log is not None else AppendOnlyDecisionLog()
        self.facility_policy_version = facility_policy_version
        self.profiles: Mapping[str, CapacityProfile] = {
            profile.facility_id: profile
            for profile in sorted(profiles, key=lambda item: item.facility_id)
        }
        self._by_id: Mapping[str, FleetAircraft] = {
            aircraft.aircraft_id: aircraft for aircraft in self.fleet
        }

    def airborne_mission_ids(self) -> frozenset[str]:
        """Missions this round may not reorder, at any priority."""
        return frozenset(
            str(aircraft.airborne_mission_id or f"mission-of-{aircraft.aircraft_id}")
            for aircraft in self.fleet
            if aircraft.is_airborne()
        )

    def run_round(  # noqa: PLR0912, PLR0915 - one round is the eleven-step flow in order
        self,
        missions: Sequence[DispatchableMission],
        *,
        as_of_us: int,
        inputs: RoundInputs,
        round_id: str = "round-1",
    ) -> DispatchRound:
        """One dispatch round over the backlog. Every path writes a record."""
        queue = DispatchQueue(policy=self.policy)
        by_id = {mission.mission_id: mission for mission in missions}
        for mission in missions:
            queue.enqueue(mission.to_queued())
        starvation = queue.starved(as_of_us)

        requests: list[AssignmentRequest] = []
        scores_by_mission: dict[str, tuple[AssignmentScore, ...]] = {}
        waits_by_pair: dict[tuple[str, str], FacilityWaits] = {}
        reposition_by_pair: dict[tuple[str, str], RepositioningCost] = {}
        rejections: dict[str, list[RejectionExplanation]] = {key: [] for key in by_id}
        blocked: set[str] = set()

        for mission_order, queued_mission in enumerate(queue.order(as_of_us)):
            mission = by_id[queued_mission.mission_id]
            row = queued_mission.as_row()
            if row.is_protected():
                rejections[mission.mission_id].append(
                    explain(
                        policy=self.policy,
                        code=ReasonCode.AIRBORNE_COMMITMENT_IMMUTABLE,
                        subject_kind="mission",
                        subject_id=mission.mission_id,
                        mission_id=mission.mission_id,
                        human_reason=(
                            f"{mission.mission_id} already holds a commitment "
                            f"({row.status.value}); the round leaves it exactly where it is"
                        ),
                        technical=(
                            ("status", row.status.value),
                            ("holds_reservation", row.holds_reservation),
                            ("rule", "priority resequences unlaunched work only"),
                        ),
                    )
                )
                continue
            query = candidate_query(
                self.fleet,
                policy=self.policy,
                as_of_us=as_of_us,
                payload_g=mission.legs.payload_g,
                charge_power_w=inputs.charge_power_w,
                latest_start_us=mission.deadline_us,
            )
            for excluded in query.excluded:
                rejections[mission.mission_id].append(
                    explain_exclusion(excluded, mission.mission_id, self.policy)
                )
            if not query.candidates:
                rejections[mission.mission_id].append(
                    explain(
                        policy=self.policy,
                        code=ReasonCode.NO_ELIGIBLE_AIRCRAFT,
                        subject_kind="mission",
                        subject_id=mission.mission_id,
                        mission_id=mission.mission_id,
                        human_reason=(
                            f"no aircraft was offered for {mission.mission_id}: "
                            f"{len(query.excluded)} tails excluded, none assignable"
                        ),
                        technical=(
                            ("excluded", ",".join(sorted(query.excluded_ids))),
                            ("query_digest", query.query_digest),
                            ("rule", "invariant I5: the candidate query is the only gate"),
                        ),
                    )
                )
                continue
            screens = screen_fleet(
                mission.legs,
                inputs.weather,
                query.candidates,
                clock_us=as_of_us,
                deadline_us=mission.deadline_us,
            )
            for infeasible in screens.infeasible:
                rejections[mission.mission_id].append(
                    explain_screen(infeasible, mission.mission_id, self.policy)
                )
            if not screens.feasible:
                continue

            scored: list[AssignmentScore] = []
            for screen in screens.feasible:
                aircraft = self._by_id[screen.aircraft_id]
                ready_us = max(as_of_us, aircraft.busy_until_us)
                decision = self.scheduler.propose(
                    TurnaroundRequest(
                        request_id=f"{mission.mission_id}@{aircraft.aircraft_id}",
                        facility_id=mission.origin_facility_id,
                        holder=mission.mission_id,
                        ready_us=ready_us,
                        deadline_us=mission.deadline_us,
                        policy_version=self.facility_policy_version,
                        durations=inputs.facility_durations,
                        requires_recharge=inputs.requires_recharge,
                        allow_queue=True,
                    )
                )
                waits = FacilityWaits()
                start_us = ready_us
                if decision.plan is not None:
                    waits = FacilityWaits.from_events(decision.plan.waits)
                    start_us = decision.plan.start_us
                if decision.state is DecisionState.REJECTED:
                    blocked.add(mission.mission_id)
                    rejections[mission.mission_id].append(
                        explain_queue(
                            mission_id=mission.mission_id,
                            aircraft_id=aircraft.aircraft_id,
                            reason=decision.reason.value,
                            detail=(
                                decision.rejection.detail
                                if decision.rejection is not None
                                else "refused by the capacity model"
                            ),
                            policy=self.policy,
                        )
                    )
                    continue
                reposition: RepositioningCost | None = None
                if decision.state is DecisionState.QUEUED:
                    reposition = self._price_alternate(
                        mission,
                        aircraft,
                        reason=decision.reason,
                        origin_wait_us=max(0, mission.deadline_us - ready_us),
                        at_us=ready_us,
                        inputs=inputs,
                    )
                    if reposition is None or reposition.units > 0:
                        blocked.add(mission.mission_id)
                        rejections[mission.mission_id].append(
                            explain_queue(
                                mission_id=mission.mission_id,
                                aircraft_id=aircraft.aircraft_id,
                                reason=decision.reason.value,
                                detail=(
                                    "capacity is full and no alternate site is cheaper than "
                                    "waiting, so this waits rather than overbooking a pad"
                                ),
                                policy=self.policy,
                            )
                        )
                        continue
                    start_us = ready_us + reposition.leg_us
                score = score_candidate(
                    mission_id=mission.mission_id,
                    aircraft_id=aircraft.aircraft_id,
                    margin_wh=screen.margin_wh,
                    earliest_start_us=start_us,
                    duration_us=mission.turnaround_us,
                    deadline_us=mission.deadline_us,
                    waits=waits,
                    policy=self.policy,
                    reposition=reposition,
                )
                scored.append(score)
                waits_by_pair[(mission.mission_id, aircraft.aircraft_id)] = waits
                if reposition is not None:
                    reposition_by_pair[(mission.mission_id, aircraft.aircraft_id)] = reposition
            if not scored:
                continue
            ranked = rank_scores(scored)
            scores_by_mission[mission.mission_id] = ranked
            for tie_index, score in enumerate(ranked):
                requests.append(
                    AssignmentRequest(
                        mission_id=score.mission_id,
                        aircraft_id=score.aircraft_id,
                        units=score.units,
                        earliest_start_us=score.earliest_start_us,
                        latest_start_us=max(
                            score.earliest_start_us,
                            mission.deadline_us - mission.turnaround_us,
                        ),
                        duration_us=score.duration_us,
                        hub_facility_id=mission.origin_facility_id,
                        energy_margin_wh=score.energy_margin_wh,
                        tie_index=mission_order * len(ranked) + tie_index,
                    )
                )

        solution = solve_assignment(
            requests,
            policy=self.policy,
            crew_capacity=inputs.crew_capacity,
            round_id=round_id,
        )
        assigned = {row.mission_id: row for row in solution.assignments}
        outcomes: list[MissionOutcome] = []
        for mission_id in sorted(by_id):
            mission = by_id[mission_id]
            fairness = queue.fairness(mission.to_queued(), as_of_us)
            chosen = assigned.get(mission_id)
            if chosen is not None:
                outcome = DecisionOutcome.ASSIGNED
            elif mission_id in blocked:
                outcome = DecisionOutcome.QUEUED
            else:
                outcome = DecisionOutcome.REJECTED
            ranked_scores = scores_by_mission.get(mission_id, ())
            key = None if chosen is None else (mission_id, chosen.aircraft_id)
            waits = FacilityWaits() if key is None else waits_by_pair.get(key, FacilityWaits())
            reposition = None if key is None else reposition_by_pair.get(key)
            record = DispatchDecision(
                decision_id=f"{round_id}:{mission_id}",
                mission_id=mission_id,
                outcome=outcome,
                as_of_us=as_of_us,
                aircraft_id=None if chosen is None else chosen.aircraft_id,
                ranked=tuple(
                    (
                        score.aircraft_id,
                        "-" if score.reposition is None else score.reposition.target_facility_id,
                        score.units,
                        score.energy_margin_wh,
                        score.lateness_us,
                    )
                    for score in ranked_scores
                ),
                rejections=tuple(rejections.get(mission_id, ())),
                fairness=fairness,
                queue_state=f"effective={fairness.effective_rank}/base={fairness.base_rank}",
                wait_reasons=waits.as_rows(),
                repositioning=() if reposition is None else reposition.metrics(),
                policy_version=self.policy.policy_version,
                fairness_policy_version=self.policy.fairness_policy_version,
                ranking_policy_version=self.policy.ranking_policy_version,
                config_release_sha=self.policy.config_release_sha,
                policy_digest=self.policy.digest(),
                solver_digest=solution.digest(),
                determinism=solution.determinism,
                proposal_digest=solution.model_digest,
                priority=mission.priority.value,
            )
            self.log.append(record)
            outcomes.append(
                MissionOutcome(
                    mission_id=mission_id,
                    outcome=outcome,
                    aircraft_id=record.aircraft_id,
                    decision=record,
                    start_us=None if chosen is None else chosen.start_us,
                    units=None if chosen is None else chosen.units,
                    waits=waits,
                    repositioning=reposition,
                )
            )

        return DispatchRound(
            round_id=round_id,
            as_of_us=as_of_us,
            outcomes=tuple(outcomes),
            solution=solution,
            starvation=starvation,
            order_after=queue.ids,
            metrics_rows=self._metrics(outcomes, solution, starvation, queue, as_of_us),
        )

    def _price_alternate(
        self,
        mission: DispatchableMission,
        aircraft: FleetAircraft,
        *,
        reason: WaitReason,
        origin_wait_us: int,
        at_us: int,
        inputs: RoundInputs,
    ) -> RepositioningCost | None:
        """Facilities described the alternates; this is where they get priced."""
        origin = self.profiles.get(mission.origin_facility_id)
        if origin is None or len(self.profiles) < 2:
            return None
        others = tuple(
            profile
            for facility_id, profile in sorted(self.profiles.items())
            if facility_id != origin.facility_id
        )
        quotes = alternate_site_quotes(
            f"{mission.mission_id}@{aircraft.aircraft_id}",
            origin,
            others,
            reason,
            self.scheduler.occupied,
            at_us=at_us,
            origin_wait_us=origin_wait_us,
        )
        profile = load_profile(aircraft.profile_id, aircraft.profile_version)
        costs = price_repositioning(
            quotes,
            inputs.legs_to_alternates.get(origin.facility_id, ()),
            policy=self.policy,
            cruise_power_w=profile.cruise_power_w,
            reference_airspeed_dmps=profile.reference_airspeed_dmps,
        )
        return best_repositioning(costs)

    def _metrics(
        self,
        outcomes: Sequence[MissionOutcome],
        solution: AssignmentSolution,
        starvation: Sequence[FairnessRank],
        queue: DispatchQueue,
        as_of_us: int,
    ) -> tuple[tuple[str, object], ...]:
        assigned = [row for row in outcomes if row.outcome is DecisionOutcome.ASSIGNED]
        rows: list[tuple[str, object]] = [
            ("as_of_us", as_of_us),
            *queue.metrics(as_of_us),
            ("assigned", len(assigned)),
            ("queued", len([row for row in outcomes if row.outcome is DecisionOutcome.QUEUED])),
            ("rejected", len([row for row in outcomes if row.outcome is DecisionOutcome.REJECTED])),
            (
                "unserved",
                len([row for row in outcomes if row.outcome is not DecisionOutcome.ASSIGNED]),
            ),
            ("starvation_events", len(starvation)),
            (
                "repositioning_energy_wh",
                sum(
                    0 if row.repositioning is None else row.repositioning.energy_wh
                    for row in outcomes
                ),
            ),
            ("solver_status", solution.status.value),
            ("solver_workers", solution.num_search_workers),
            ("solver_seed", solution.random_seed),
        ]
        for name in ("pad_wait_us", "crew_wait_us", "charger_wait_us"):
            rows.append(
                (
                    name,
                    sum(dict(row.waits.as_rows()).get(name, 0) for row in assigned),
                )
            )
        return tuple(rows)


@dataclass(frozen=True)
class RunOutcome:
    """What one strategy did with one seed of identical demand."""

    strategy: str
    seed: int
    completed: int
    late: int
    unserved: int
    rejected: int
    energy_wh: int
    mean_lateness_us: int
    pad_wait_us: int
    crew_wait_us: int
    charger_wait_us: int
    crossings: int
    recompute_attempts: int
    cost_units: int
    demand_digest: str
    inputs_digest: str
    fleet_size: int
    pad_capacity: int
    charger_capacity: int
    crew_capacity: int
    cutoff_us: int
    solver_workers: int
    energy_profile_digest: str

    def guarded(self) -> Mapping[str, object]:
        return {name: getattr(self, name) for name in GUARDED_INPUT_FIELDS}

    def metrics(self) -> Mapping[str, int]:
        return {
            "completed": self.completed,
            "late": self.late,
            "unserved": self.unserved,
            "rejected": self.rejected,
            "energy_wh": self.energy_wh,
            "mean_lateness_us": self.mean_lateness_us,
            "pad_wait_us": self.pad_wait_us,
            "crew_wait_us": self.crew_wait_us,
            "charger_wait_us": self.charger_wait_us,
            "crossings": self.crossings,
            "recompute_attempts": self.recompute_attempts,
            "cost_units": self.cost_units,
        }


class StrategyRunner(Protocol):
    """The strategies under test, supplied by the composition layer."""

    def run(self, strategy: str, *, seed: int) -> RunOutcome: ...


@dataclass(frozen=True)
class MetricSummary:
    """Median and spread over the seeded repetitions - never a single lucky run."""

    metric: str
    samples: int
    median: int
    minimum: int
    maximum: int
    spread: int
    mean: int

    def as_row(self) -> tuple[str, int, int, int, int, int, int]:
        return (
            self.metric,
            self.samples,
            self.median,
            self.minimum,
            self.maximum,
            self.spread,
            self.mean,
        )


def median_low(values: Sequence[int]) -> int:
    """Lower median, in integers, because these are watt-hours and microseconds."""
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[(len(ordered) - 1) // 2]


def summarise(metric: str, values: Sequence[int]) -> MetricSummary:
    rows = list(values)
    return MetricSummary(
        metric=metric,
        samples=len(rows),
        median=median_low(rows),
        minimum=min(rows) if rows else 0,
        maximum=max(rows) if rows else 0,
        spread=(max(rows) - min(rows)) if rows else 0,
        mean=sum(rows) // len(rows) if rows else 0,
    )


@dataclass(frozen=True)
class StrategyReport:
    strategy: str
    repetitions: int
    summaries: tuple[MetricSummary, ...]
    runs: tuple[RunOutcome, ...]

    def summary_of(self, metric: str) -> MetricSummary:
        for row in self.summaries:
            if row.metric == metric:
                return row
        raise KeyError(metric)

    def median(self, metric: str) -> int:
        return self.summary_of(metric).median

    def spread(self, metric: str) -> int:
        return self.summary_of(metric).spread


@dataclass(frozen=True)
class ComparisonReport:
    """The comparison, with the proof that it was fair attached."""

    strategies: tuple[StrategyReport, ...]
    seeds: tuple[int, ...]
    guarded_fields: tuple[str, ...]
    checks: tuple[tuple[str, str], ...]

    def of(self, strategy: str) -> StrategyReport:
        for row in self.strategies:
            if row.strategy == strategy:
                return row
        raise KeyError(strategy)

    def table(self, metrics: Sequence[str] = RANK_METRICS) -> tuple[tuple[str, ...], ...]:
        header = ("strategy", *(f"{name}: median/spread (min-max)" for name in metrics))
        rows: list[tuple[str, ...]] = [header]
        for report in self.strategies:
            cells: list[str] = [report.strategy]
            for name in metrics:
                summary = report.summary_of(name)
                cells.append(
                    f"{summary.median}/{summary.spread} "
                    f"({summary.minimum}-{summary.maximum}, n={summary.samples})"
                )
            rows.append(tuple(cells))
        return tuple(rows)

    def render(self, metrics: Sequence[str] = RANK_METRICS) -> str:
        rows = self.table(metrics)
        widths = [max(len(str(row[index])) for row in rows) for index in range(len(rows[0]))]
        lines = [
            "  ".join(str(cell).ljust(widths[index]) for index, cell in enumerate(row))
            for row in rows
        ]
        return "\n".join(lines)

    def as_document(self, metrics: Sequence[str] = RANK_METRICS) -> Mapping[str, object]:
        return {
            "seeds": list(self.seeds),
            "repetitions": len(self.seeds),
            "guarded_input_fields": list(self.guarded_fields),
            "fairness_checks": dict(self.checks),
            "banner": "Simulation only. Not authorised for flight.",
            "strategies": {
                report.strategy: {
                    summary.metric: list(summary.as_row()) for summary in report.summaries
                }
                for report in self.strategies
            },
            "table": [list(row) for row in self.table(metrics)],
        }


class ScenarioComparison:
    """Run N strategies over the same demand for M seeds and refuse an unfair race."""

    def __init__(
        self,
        *,
        runner: StrategyRunner,
        strategies: Sequence[str],
        seeds: Sequence[int],
        policy: DispatchPolicy,
    ) -> None:
        if len(seeds) < MIN_COMPARISON_REPETITIONS:
            raise InsufficientRepetitionsError(
                f"a comparison needs at least {MIN_COMPARISON_REPETITIONS} seeded "
                f"repetitions per strategy; got {len(seeds)}"
            )
        if len(set(seeds)) != len(seeds):
            raise InsufficientRepetitionsError("duplicate seeds are not repetitions")
        if not strategies or len(set(strategies)) != len(strategies):
            raise DispatchInputError("each strategy may appear once, and at least one is needed")
        self.runner = runner
        self.strategy_names = tuple(sorted(strategies))
        self.seeds = tuple(sorted(seeds))
        self.policy = policy

    def run(self) -> ComparisonReport:
        """Identical demand per seed across arms; median and spread per strategy."""
        runs: list[RunOutcome] = []
        for seed in self.seeds:
            per_seed: list[RunOutcome] = []
            for strategy in self.strategy_names:
                outcome = self.runner.run(strategy, seed=seed)
                if outcome.strategy != strategy or outcome.seed != seed:
                    raise WeakenedBaselineError(
                        f"runner returned {outcome.strategy}/seed {outcome.seed} for "
                        f"{strategy}/{seed}: the arms are not comparable"
                    )
                per_seed.append(outcome)
            self._assert_identical(seed, per_seed)
            runs.extend(per_seed)
        by_strategy: dict[str, list[RunOutcome]] = {}
        for outcome in runs:
            by_strategy.setdefault(outcome.strategy, []).append(outcome)
        reports: list[StrategyReport] = []
        for strategy in self.strategy_names:
            rows = by_strategy.get(strategy, [])
            names = tuple(sorted(rows[0].metrics())) if rows else ()
            reports.append(
                StrategyReport(
                    strategy=strategy,
                    repetitions=len(rows),
                    summaries=tuple(
                        summarise(name, [row.metrics()[name] for row in rows]) for name in names
                    ),
                    runs=tuple(rows),
                )
            )
        checks = (
            ("identical_demand", "every seed gave every strategy the same demand digest"),
            (
                "identical_capacity",
                "fleet, pad, charger, crew, energy profile and cutoff matched per seed",
            ),
            (
                "one_solver_worker",
                f"every arm answered with {ONE_WORKER} search worker",
            ),
            (
                "repetitions",
                f"{len(self.seeds)} seeds x {len(self.strategy_names)} strategies"
                f" = {len(runs)} runs",
            ),
        )
        return ComparisonReport(
            strategies=tuple(reports),
            seeds=self.seeds,
            guarded_fields=GUARDED_INPUT_FIELDS,
            checks=checks,
        )

    def _assert_identical(self, seed: int, rows: Sequence[RunOutcome]) -> None:
        first = rows[0]
        left = first.guarded()
        for outcome in rows[1:]:
            right = outcome.guarded()
            for name in GUARDED_INPUT_FIELDS:
                if left[name] != right[name]:
                    raise WeakenedBaselineError(
                        f"seed {seed}: {first.strategy} saw {name}={left[name]!r} while "
                        f"{outcome.strategy} saw {name}={right[name]!r} - a weakened "
                        "baseline is a misleading comparison, not a conservative one"
                    )
        for outcome in rows:
            if outcome.solver_workers != ONE_WORKER:
                raise WeakenedBaselineError(
                    f"seed {seed}: {outcome.strategy} solved with {outcome.solver_workers} "
                    "workers; that answer would not replay"
                )


__all__ = [
    "GUARDED_INPUT_FIELDS",
    "MIN_COMPARISON_REPETITIONS",
    "ONE_WORKER",
    "RANK_METRICS",
    "ComparisonReport",
    "DispatchRound",
    "DispatchableMission",
    "Dispatcher",
    "MetricSummary",
    "MissionOutcome",
    "RoundInputs",
    "RunOutcome",
    "ScenarioComparison",
    "StrategyReport",
    "StrategyRunner",
    "airborne_ids_of",
    "median_low",
    "summarise",
]


def airborne_ids_of(fleet: Sequence[FleetAircraft]) -> frozenset[str]:
    """Mission ids that no priority class may reorder, from a fleet view."""
    return frozenset(
        str(aircraft.airborne_mission_id or f"mission-of-{aircraft.aircraft_id}")
        for aircraft in fleet
        if aircraft.is_airborne()
    )
