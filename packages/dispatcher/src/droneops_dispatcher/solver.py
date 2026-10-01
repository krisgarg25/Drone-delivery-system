"""OR-Tools assignment and time-window layer. One worker, one seed, one answer.

What the model is
    A dispatch round. Candidates are ``(mission, aircraft)`` pairs already screened
    by :func:`droneops_dispatcher.ranking.screen_fleet` (invariant I4) and priced by
    :func:`droneops_dispatcher.ranking.score_candidate`. The solver chooses a subset
    such that: each mission gets at most one aircraft; each aircraft gets at most one
    mission in the round; a chosen pair starts inside its declared window; and the
    overlapping windows at a hub respect that hub's declared crew capacity, which is
    where queueing rather than overbooking comes from (invariant I6). The objective
    minimises total cost units plus a penalty for each mission left unserved, so the
    round flies work when it honestly can and refuses when it cannot.

Why the settings are frozen in code (read this before "improving" throughput)
    ``num_search_workers`` is set to exactly **one** and ``random_seed`` to the
    policy's fixed seed, on the *solver protocol object*, and the solution reports
    the values that were actually applied. With one worker CP-SAT runs a single
    deterministic search thread, so the same model yields the same objective and the
    same assignment on every run and every machine. With
    ``num_search_workers > 1`` CP-SAT races a portfolio of complementary strategies
    and keeps whichever answer arrives first: the winner becomes *timing dependent*.
    That is not a throughput knob - it changes which aircraft the operator is
    offered, so replays, golden comparisons and incident reviews stop reproducing,
    and non-determinism is a defect that fails the CI replay gate
    (``conventions.md`` section 4, invariant I7). :class:`SolverSettings` therefore
    refuses any other worker count at construction, and
    ``tests/test_phase3_exit_criteria.py`` asserts both the declared and the applied
    worker count on a real solution.

Determinism beyond the worker count
    Equal-cost candidates are broken by ``tie_index`` - the position the fairness and
    ranking layers already gave the pair - scaled into the objective by a spread
    strictly larger than any tie index. Cost dominates the tie-break, never the other
    way round, and the optimum is unique, so "which candidate won" cannot depend on
    search order either.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from ortools.sat.python import cp_model

from .errors import DispatchInputError
from .policy import ONE_SOLVER_WORKER, DispatchPolicy, digest_of

NO_CAPACITY_DECLARED: str = "hub_capacity_undeclared"


class SolverStatus(StrEnum):
    OPTIMAL = "optimal"
    FEASIBLE = "feasible"
    INFEASIBLE = "infeasible"
    NOTHING_TO_SOLVE = "nothing_to_solve"

    @property
    def answered(self) -> bool:
        return self in (SolverStatus.OPTIMAL, SolverStatus.FEASIBLE)


@dataclass(frozen=True)
class AssignmentRequest:
    """One screened candidate pair, in solver terms."""

    mission_id: str
    aircraft_id: str
    units: int
    earliest_start_us: int
    latest_start_us: int
    duration_us: int
    hub_facility_id: str
    energy_margin_wh: int = 0
    tie_index: int = 0

    def __post_init__(self) -> None:
        if self.duration_us <= 0:
            raise DispatchInputError(
                f"{self.mission_id}/{self.aircraft_id}: a turn taking no time is a bug"
            )
        if self.latest_start_us < self.earliest_start_us:
            raise DispatchInputError(f"{self.mission_id}/{self.aircraft_id}: inverted start window")

    def window_is_satisfiable(self) -> bool:
        return self.earliest_start_us <= self.latest_start_us

    def digest(self) -> str:
        return digest_of(
            [
                ("mission_id", self.mission_id),
                ("aircraft_id", self.aircraft_id),
                ("units", self.units),
                ("earliest_start_us", self.earliest_start_us),
                ("latest_start_us", self.latest_start_us),
                ("duration_us", self.duration_us),
                ("hub_facility_id", self.hub_facility_id),
                ("energy_margin_wh", self.energy_margin_wh),
                ("tie_index", self.tie_index),
            ]
        )


@dataclass(frozen=True)
class Assignment:
    mission_id: str
    aircraft_id: str
    start_us: int
    end_us: int
    units: int
    energy_margin_wh: int
    hub_facility_id: str

    def as_row(self) -> tuple[str, str, int, int, int]:
        return (self.mission_id, self.aircraft_id, self.start_us, self.end_us, self.units)


@dataclass(frozen=True)
class AssignmentSolution:
    """The round's answer, with the settings that made it reproducible."""

    round_id: str
    status: SolverStatus
    solved_to_optimum: bool
    assignments: tuple[Assignment, ...]
    unassigned: tuple[tuple[str, str], ...]
    objective_units: int
    num_search_workers: int
    random_seed: int
    max_time_in_seconds: int
    model_stats: tuple[tuple[str, int], ...]
    determinism: tuple[tuple[str, str], ...]
    policy_version: str
    solver_settings_digest: str
    model_digest: str
    ranking_digest: str

    def assigned_missions(self) -> frozenset[str]:
        return frozenset(row.mission_id for row in self.assignments)

    def aircraft_used(self) -> frozenset[str]:
        return frozenset(row.aircraft_id for row in self.assignments)

    def ranking_rows(self) -> tuple[tuple[str, str, int, int, int], ...]:
        """The byte-comparable ranking: mission, aircraft, start, units, margin."""
        return tuple(
            sorted(
                (row.mission_id, row.aircraft_id, row.start_us, row.units, row.energy_margin_wh)
                for row in self.assignments
            )
        )

    def is_reproducible_with(self, other: AssignmentSolution) -> bool:
        return (
            self.ranking_digest == other.ranking_digest
            and self.model_digest == other.model_digest
            and self.solver_settings_digest == other.solver_settings_digest
        )

    def digest(self) -> str:
        return digest_of(
            [
                ("round_id", self.round_id),
                ("status", self.status.value),
                (
                    "ranking",
                    ";".join("|".join(str(part) for part in row) for row in self.ranking_rows()),
                ),
                (
                    "unassigned",
                    ";".join(f"{mission}:{reason}" for mission, reason in self.unassigned),
                ),
                ("objective_units", self.objective_units),
                ("solver_settings", self.solver_settings_digest),
                ("policy_version", self.policy_version),
            ]
        )


def _model_digest(requests: Sequence[AssignmentRequest], capacity: Mapping[str, int]) -> str:
    rows: list[tuple[str, object]] = [("request", item.digest()) for item in requests]
    rows.extend((f"crew_capacity_{hub}", value) for hub, value in sorted(capacity.items()))
    return digest_of(rows)


def solve_assignment(
    requests: Sequence[AssignmentRequest],
    *,
    policy: DispatchPolicy,
    crew_capacity: Mapping[str, int],
    round_id: str,
) -> AssignmentSolution:
    """Assign missions to aircraft inside their windows, without overbooking a hub."""
    settings = policy.solver
    determinism = tuple((key, value) for key, value in policy.determinism())
    ordered = sorted(requests, key=lambda item: (item.tie_index, item.mission_id, item.aircraft_id))
    feasible = [item for item in ordered if item.window_is_satisfiable()]
    skipped = tuple(
        sorted(
            (item.mission_id, "time_window_infeasible") for item in ordered if item not in feasible
        )
    )
    model_digest = _model_digest(ordered, crew_capacity)
    missions = sorted({item.mission_id for item in ordered})
    penalty = policy.unserved_penalty_units

    if not feasible:
        return AssignmentSolution(
            round_id=round_id,
            status=SolverStatus.NOTHING_TO_SOLVE,
            solved_to_optimum=True,
            assignments=(),
            unassigned=tuple(sorted(set(skipped))),
            objective_units=penalty * len(missions),
            num_search_workers=settings.num_search_workers,
            random_seed=settings.random_seed,
            max_time_in_seconds=settings.max_time_in_seconds,
            model_stats=(("requests", len(ordered)), ("screened_out", len(skipped))),
            determinism=determinism,
            policy_version=policy.policy_version,
            solver_settings_digest=settings.digest(),
            model_digest=model_digest,
            ranking_digest=digest_of([("empty", round_id)]),
        )

    # OR-Tools publishes no stubs for the CamelCase model API (mypy can only see the
    # snake_case internals), so this block is the one explicitly marked untyped
    # boundary in the package - the repo's convention for third-party seams. The
    # values that leave it are ints and strings, never solver objects.
    model = cp_model.CpModel()
    presence = [model.NewBoolVar(f"x{index}") for index in range(len(feasible))]  # type: ignore[attr-defined]
    starts = [
        model.NewIntVar(item.earliest_start_us, item.latest_start_us, f"s{index}")  # type: ignore[attr-defined]
        for index, item in enumerate(feasible)
    ]
    ends = [
        model.NewIntVar(  # type: ignore[attr-defined]
            item.earliest_start_us + item.duration_us,
            item.latest_start_us + item.duration_us,
            f"e{index}",
        )
        for index, item in enumerate(feasible)
    ]
    for index, item in enumerate(feasible):
        # A chosen pair starts where the window says and holds the slot for its turn.
        model.Add(ends[index] == starts[index] + item.duration_us).OnlyEnforceIf(presence[index])  # type: ignore[attr-defined]

    for mission in missions:
        chosen = [presence[i] for i, item in enumerate(feasible) if item.mission_id == mission]
        model.AddAtMostOne(chosen)  # type: ignore[attr-defined]
    for tail in sorted({item.aircraft_id for item in feasible}):
        chosen = [presence[i] for i, item in enumerate(feasible) if item.aircraft_id == tail]
        model.AddAtMostOne(chosen)  # type: ignore[attr-defined]

    undeclared = 0
    for hub in sorted({item.hub_facility_id for item in feasible}):
        capacity_units = crew_capacity.get(hub)
        index_at = [i for i, item in enumerate(feasible) if item.hub_facility_id == hub]
        if capacity_units is None:
            undeclared += 1
            continue
        intervals = [
            model.NewOptionalIntervalVar(  # type: ignore[attr-defined]
                starts[i], feasible[i].duration_us, ends[i], presence[i], f"iv{i}"
            )
            for i in index_at
        ]
        demands = [1] * len(intervals)
        # I6 lives here: the hub's declared crew slots are a hard ceiling on
        # overlapping turns, and work that does not fit goes unserved this round.
        model.AddCumulative(intervals, demands, max(1, capacity_units))  # type: ignore[attr-defined]

    # A spread strictly larger than any tie index: cost dominates, the tie-break only
    # orders equal-cost choices, so the optimum is unique.
    spread = len(feasible) + 1
    coefficients = [item.units * spread + item.tie_index - penalty * spread for item in feasible]
    model.Minimize(cp_model.LinearExpr.WeightedSum(presence, coefficients))  # type: ignore[attr-defined]

    solver = cp_model.CpSolver()
    solver.parameters.num_search_workers = settings.num_search_workers
    solver.parameters.random_seed = settings.random_seed
    solver.parameters.max_time_in_seconds = settings.max_time_in_seconds
    solver.parameters.log_search_progress = settings.log_search_progress
    status_name = solver.StatusName(solver.Solve(model))
    status = {
        "OPTIMAL": SolverStatus.OPTIMAL,
        "FEASIBLE": SolverStatus.FEASIBLE,
    }.get(status_name, SolverStatus.INFEASIBLE)

    chosen_rows: list[Assignment] = []
    if status.answered:
        for index, item in enumerate(feasible):
            if int(solver.Value(presence[index])) == 1:
                start_us = int(solver.Value(starts[index]))
                chosen_rows.append(
                    Assignment(
                        mission_id=item.mission_id,
                        aircraft_id=item.aircraft_id,
                        start_us=start_us,
                        end_us=start_us + item.duration_us,
                        units=item.units,
                        energy_margin_wh=item.energy_margin_wh,
                        hub_facility_id=item.hub_facility_id,
                    )
                )
    chosen_rows.sort(key=lambda row: (row.mission_id, row.aircraft_id))
    served = {row.mission_id for row in chosen_rows}
    refused = dict.fromkeys(
        [
            *skipped,
            *[(mission, status.value) for mission in missions if mission not in served],
        ]
    )
    ranking_rows = tuple(
        sorted(
            (row.mission_id, row.aircraft_id, row.start_us, row.units, row.energy_margin_wh)
            for row in chosen_rows
        )
    )
    stats = (
        ("missions", len(missions)),
        ("aircraft", len({item.aircraft_id for item in feasible})),
        ("candidate_pairs", len(feasible)),
        ("assigned", len(chosen_rows)),
        ("unserved", len([mission for mission in missions if mission not in served])),
        ("screened_out", len(skipped)),
        ("hubs", len({item.hub_facility_id for item in feasible})),
        ("hubs_with_undeclared_capacity", undeclared),
        ("branches", int(solver.NumBranches())),
        ("conflicts", int(solver.NumConflicts())),
    )
    return AssignmentSolution(
        round_id=round_id,
        status=status,
        solved_to_optimum=status is SolverStatus.OPTIMAL,
        assignments=tuple(chosen_rows),
        unassigned=tuple(sorted(refused)),
        objective_units=sum(row.units for row in chosen_rows)
        + penalty * len([mission for mission in missions if mission not in served]),
        num_search_workers=int(solver.parameters.num_search_workers),
        random_seed=int(solver.parameters.random_seed),
        max_time_in_seconds=int(solver.parameters.max_time_in_seconds),
        model_stats=stats,
        determinism=determinism,
        policy_version=policy.policy_version,
        solver_settings_digest=settings.digest(),
        model_digest=model_digest,
        ranking_digest=digest_of(
            [("rank", "|".join(str(part) for part in row)) for row in ranking_rows]
        ),
    )


__all__ = [
    "NO_CAPACITY_DECLARED",
    "ONE_SOLVER_WORKER",
    "Assignment",
    "AssignmentRequest",
    "AssignmentSolution",
    "SolverStatus",
    "solve_assignment",
]
