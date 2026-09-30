"""planner: the pure candidate-plan pipeline.

Owns the 13-step flow from an order to a *proposal* (``technical-build/11`` section
3). It does not own reservation authority, conflict adjudication or any checker
internals: the proposal it returns carries no ``valid`` / ``safe`` field, because
only the reservation ledger commits and only the independent checker proves
(``docs/conventions.md`` section 6).

Deterministic by construction: integer microseconds everywhere, integer watt-hours,
basis-point ratios, no wall clock, no random value without a recorded seed, and
every sequence emitted in a documented total order.

External behaviour arrives through the ports in
:mod:`droneops_planner.ports` - the swept-volume builder is the **geom-4d seam**,
the occupancy and commit ports are the reservation ledger, the energy and facility
ports are their own packages - and ``defaults.py`` ships reference implementations
so steps 5 to 13 are real computations today.
"""

from __future__ import annotations

from .compare import ComparisonInputs, StrategyComparator, StrategyMetrics, identical_inputs
from .defaults import (
    BatteryCurveEnergyModel,
    BundleFacilityScheduler,
    CellSweepVolumeBuilder,
    FrozenWindTrace,
    InMemoryCommitPort,
    InMemoryOccupancy,
)
from .pipeline import (
    CandidatePlanPipeline,
    PipelineConfig,
    PipelinePorts,
    default_policy,
)
from .ports import (
    CommitPort,
    EnergyModel,
    FacilityScheduler,
    OccupancySource,
    SweptVolumeBuilder,
    VolumePadding,
    WindSource,
)
from .reasons import (
    PipelineConfigError,
    PlannerError,
    ReasonCode,
    Rejection,
    RequestValidationError,
    human_reason,
)
from .space_time import (
    EdgeTiming,
    OverlapReport,
    RouteTiming,
    build_volumes,
    chain_route_timing,
    prefilter_and_intersect,
    safe_intervals_for_edge,
)
from .strategies import (
    ConstraintAwareDirectStrategy,
    HybridRoutingStrategy,
    RoutingStrategy,
    StrategyRegistry,
    StructuredCorridorStrategy,
    connector_lattice,
    identical_inputs_digest,
)
from .types import (
    AircraftEnvelope,
    AircraftState,
    CandidatePlan,
    CommitOutcome,
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
    PriorityClass,
    ProtectedVolume,
    RetryRecord,
    RouteShape,
    Score,
    StepRecord,
    TerminalLeg,
)

__all__ = [
    "AircraftEnvelope",
    "AircraftState",
    "BatteryCurveEnergyModel",
    "BundleFacilityScheduler",
    "CandidatePlan",
    "CandidatePlanPipeline",
    "CellSweepVolumeBuilder",
    "CommitOutcome",
    "CommitPort",
    "CommitRequest",
    "CommitState",
    "ComparisonInputs",
    "ConstraintAwareDirectStrategy",
    "DecisionTrace",
    "EdgeTiming",
    "EligibleCandidate",
    "EnergyEvaluation",
    "EnergyModel",
    "FacilityScheduler",
    "FrozenWindTrace",
    "HybridRoutingStrategy",
    "InMemoryCommitPort",
    "InMemoryOccupancy",
    "LegKind",
    "OccupancySource",
    "OverlapReport",
    "PadWindow",
    "PipelineConfig",
    "PipelineConfigError",
    "PipelinePorts",
    "PlanPoint",
    "PlanProposal",
    "PlanRequest",
    "PlannerError",
    "PolicySnapshot",
    "PriorityClass",
    "ProtectedVolume",
    "ReasonCode",
    "Rejection",
    "RequestValidationError",
    "RetryRecord",
    "RouteShape",
    "RouteTiming",
    "RoutingStrategy",
    "Score",
    "StepRecord",
    "StrategyComparator",
    "StrategyMetrics",
    "StrategyRegistry",
    "StructuredCorridorStrategy",
    "SweptVolumeBuilder",
    "TerminalLeg",
    "VolumePadding",
    "WindSource",
    "build_volumes",
    "chain_route_timing",
    "connector_lattice",
    "default_policy",
    "human_reason",
    "identical_inputs",
    "identical_inputs_digest",
    "prefilter_and_intersect",
    "safe_intervals_for_edge",
]
