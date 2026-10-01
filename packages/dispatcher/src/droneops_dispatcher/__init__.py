"""dispatcher: aircraft assignment, priority and queue fairness (Phase 3).

What this package owns (``conventions.md`` section 7): **assignment, priority,
fairness** - and nothing else. It never commits a reservation (the ledger does,
``conventions.md`` section 5), never re-derives an energy margin (``droneops_energy``
does, invariant I4), never re-models a pad (``droneops_facilities`` does, invariant
I6), and cannot reach an aircraft at all: ``.importlinter`` keeps *Dispatcher cannot
reach a vehicle link* enforced, so an airborne "change" here is a proposal an
operator accepts (:mod:`droneops_dispatcher.airborne`), not a command.

The pieces, in the order a round uses them
    :class:`~droneops_dispatcher.policy.DispatchPolicy`
        every tunable, versioned: the named ``starvation_bound_us``, the aging step,
        the ranking weights, and the solver settings that must stay at one worker.
    :func:`~droneops_dispatcher.eligibility.candidate_query`
        invariant I5 as a query. Offline and maintenance tails are not ranked last;
        they are absent from the candidate set, with an explanation for each.
    :func:`~droneops_dispatcher.ranking.screen_fleet`
        invariant I4 per candidate: the complete mission (outbound, handover,
        recovery, reserve) against one battery, as a signed margin.
    :func:`~droneops_dispatcher.ranking.price_repositioning`
        the ``priced_by="dispatcher"`` data ``droneops_facilities`` handed over,
        priced from the airframe's own versioned profile.
    :func:`~droneops_dispatcher.solver.solve_assignment`
        OR-Tools CP-SAT over windows and hub capacity, one worker, fixed seed.
    :class:`~droneops_dispatcher.queue.DispatchQueue`
        fairness order, aging, the starvation bound, and resequencing that pins
        anything airborne or already committed.
    :mod:`~droneops_dispatcher.priority`
        four kinds of "important" with four different power sets.
    :mod:`~droneops_dispatcher.rejection`
        human-readable plus technical reason, persisted, for every refusal.
    :class:`~droneops_dispatcher.dispatch.Dispatcher`
        the round, and :class:`~droneops_dispatcher.dispatch.ScenarioComparison`
        for the seeded strategy comparison that refuses a weakened baseline.

Simulation only. Not authorised for flight.
"""

from __future__ import annotations

from .airborne import (
    COMMAND_AUTHORITY,
    AcceptanceRecord,
    AcceptanceToken,
    AirborneChangeProposal,
    AlternativeOption,
    ChangePreview,
    ProposalState,
    SafetyParameters,
    draft_airborne_change,
    register_of,
)
from .dispatch import (
    GUARDED_INPUT_FIELDS,
    MIN_COMPARISON_REPETITIONS,
    ONE_WORKER,
    RANK_METRICS,
    airborne_ids_of,
)
from .dispatch import (
    ComparisonReport,
    DispatchRound,
    DispatchableMission,
    Dispatcher,
    MetricSummary,
    MissionOutcome,
    RoundInputs,
    RunOutcome,
    ScenarioComparison,
    StrategyReport,
    StrategyRunner,
    median_low,
    summarise,
)
from .eligibility import (
    ASSIGNABLE_STATES,
    BUSY_STATES,
    NEVER_ASSIGNABLE,
    OPERABLE_FLOOR,
    RELIABLE_LINKS,
    EligibilityQuery,
    ExcludedAircraft,
    FleetAircraft,
    IneligibleReason,
    candidate_query,
    protected_tails,
)
from .errors import (
    CommitmentImmutableError,
    DispatchInputError,
    DispatcherError,
    EligibilityQueryError,
    FairnessPolicyError,
    IdentityRequiredError,
    InsufficientRepetitionsError,
    PolicyVersionError,
    ProposalStateError,
    SolverDeterminismError,
    WeakenedBaselineError,
)
from .policy import (
    DEFAULT_AGING_STEP_US,
    DEFAULT_STARVATION_BOUND_US,
    DETERMINISM_ROWS,
    DISPATCH_POLICY_VERSION,
    FAIRNESS_POLICY_VERSION,
    MICROS_PER_HOUR,
    MICROS_PER_MINUTE,
    MICROS_PER_SECOND,
    ONE_SOLVER_WORKER,
    RANKING_POLICY_VERSION,
    SIM_ONLY_BANNER,
    STARVATION_BOUND_NAME,
    DispatchPolicy,
    SolverSettings,
    digest_of,
)
from .priority import (
    AIRBORNE_STATUSES,
    EMERGENCY_RIGHTS,
    PRIORITY_ORDER,
    PRIORITY_POWERS,
    PROTECTED_STATUSES,
    UNLAUNCHED_STATUSES,
    MissionStatusRow,
    PriorityPowers,
    PriorityRight,
    ResequenceRefusal,
    airborne_change_authority,
    life_critical_floor,
    ordinary_rank,
    powers_digest,
    powers_of,
    priority_rank,
    protection_verdict,
    withdrawal_is_legal,
)
from .queue import DispatchQueue, FairnessRank, QueuedMission, ResequenceOutcome, queued_from
from .ranking import (
    INCOMPARABLE_EXCLUDED,
    INCOMPARABLE_NO_GEOMETRY,
    AssignmentScore,
    EnergyScreen,
    EnergyScreenResult,
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
    DISPATCHER_ACTOR,
    AppendOnlyDecisionLog,
    DecisionOutcome,
    DispatchDecision,
    ReasonCode,
    RejectionExplanation,
    explain,
    explain_exclusion,
    explain_priority,
    explain_queue,
    explain_screen,
    explain_solver,
)
from .solver import (
    NO_CAPACITY_DECLARED,
    Assignment,
    AssignmentRequest,
    AssignmentSolution,
    SolverStatus,
    solve_assignment,
)

__all__ = [
    # ---- constants
    "AIRBORNE_STATUSES",
    "ASSIGNABLE_STATES",
    "BUSY_STATES",
    "COMMAND_AUTHORITY",
    "DEFAULT_AGING_STEP_US",
    "DEFAULT_STARVATION_BOUND_US",
    "DETERMINISM_ROWS",
    "DISPATCH_POLICY_VERSION",
    "DISPATCHER_ACTOR",
    "EMERGENCY_RIGHTS",
    "FAIRNESS_POLICY_VERSION",
    "GUARDED_INPUT_FIELDS",
    "INCOMPARABLE_EXCLUDED",
    "INCOMPARABLE_NO_GEOMETRY",
    "MICROS_PER_HOUR",
    "MICROS_PER_MINUTE",
    "MICROS_PER_SECOND",
    "MIN_COMPARISON_REPETITIONS",
    "NEVER_ASSIGNABLE",
    "ONE_SOLVER_WORKER",
    "ONE_WORKER",
    "OPERABLE_FLOOR",
    "PRIORITY_ORDER",
    "PRIORITY_POWERS",
    "PROTECTED_STATUSES",
    "RANKING_POLICY_VERSION",
    "RANK_METRICS",
    "RELIABLE_LINKS",
    "SIM_ONLY_BANNER",
    "STARVATION_BOUND_NAME",
    "UNLAUNCHED_STATUSES",
    # ---- policy and errors
    "DispatchPolicy",
    "SolverSettings",
    "CommitmentImmutableError",
    "DispatchInputError",
    "DispatcherError",
    "EligibilityQueryError",
    "FairnessPolicyError",
    "IdentityRequiredError",
    "InsufficientRepetitionsError",
    "PolicyVersionError",
    "ProposalStateError",
    "SolverDeterminismError",
    "WeakenedBaselineError",
    "digest_of",
    # ---- eligibility (I5)
    "EligibilityQuery",
    "ExcludedAircraft",
    "FleetAircraft",
    "IneligibleReason",
    "candidate_query",
    "protected_tails",
    # ---- ranking, energy (I4), repositioning
    "AssignmentScore",
    "EnergyScreen",
    "EnergyScreenResult",
    "FacilityWaits",
    "MissionLegs",
    "RepositioningCost",
    "RepositioningLeg",
    "best_repositioning",
    "price_repositioning",
    "rank_scores",
    "score_candidate",
    "screen_fleet",
    # ---- solver
    "NO_CAPACITY_DECLARED",
    "Assignment",
    "AssignmentRequest",
    "AssignmentSolution",
    "SolverStatus",
    "solve_assignment",
    # ---- queue and priority
    "DispatchQueue",
    "FairnessRank",
    "QueuedMission",
    "ResequenceOutcome",
    "queued_from",
    "MissionStatusRow",
    "PriorityPowers",
    "PriorityRight",
    "ResequenceRefusal",
    "airborne_change_authority",
    "life_critical_floor",
    "ordinary_rank",
    "powers_digest",
    "powers_of",
    "priority_rank",
    "protection_verdict",
    "withdrawal_is_legal",
    # ---- airborne protocol
    "AcceptanceRecord",
    "AcceptanceToken",
    "AirborneChangeProposal",
    "AlternativeOption",
    "ChangePreview",
    "ProposalState",
    "SafetyParameters",
    "draft_airborne_change",
    "register_of",
    # ---- records
    "AppendOnlyDecisionLog",
    "DecisionOutcome",
    "DispatchDecision",
    "ReasonCode",
    "RejectionExplanation",
    "explain",
    "explain_exclusion",
    "explain_priority",
    "explain_queue",
    "explain_screen",
    "explain_solver",
    # ---- round and comparison
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
