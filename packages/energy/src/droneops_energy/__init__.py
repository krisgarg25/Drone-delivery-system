"""DroneOps energy and recovery feasibility kernel (Phase 3).

What this package is: a versioned, integer-arithmetic model of what a mission
costs in watt-hours, and a *signed margin* for how far it is from the edge. It
answers the energy half of "which aircraft can realistically fly this?" for
``packages/dispatcher`` and stops there: no queueing, no priority, no assignment.

The boundaries that shape the design:

* **I4, complete mission.** Outbound + handover + recovery-or-alternate + reserve
  are charged against one battery. A mission whose outbound leg fits but whose
  recovery does not is rejected, and the report names the term that broke it.
* **A margin, not a boolean.** ``margin_wh`` is signed so dispatch can rank plans
  and the console can show how close a decision was to the edge.
* **Frozen weather only.** The kernel accepts a :class:`WindSnapshot` object, never
  a client, URL or clock, so a deterministic run cannot fetch. Enforced again by
  ``tests/test_energy_purity_guard.py``, which scans this package's source.
* **Illustrative basis, always stated.** Every battery curve, mass effect, wind
  coefficient and degradation figure comes from a named, content-hashed profile
  version, and every output repeats that basis. No number here is vendor or
  measured pilot data.
* **Battery model disagreement is a review trigger**, not an acceptance
  (:func:`compare_battery_models`).

Simulation only. Not authorised for flight.
"""

from __future__ import annotations

from .crosscheck import (
    DEFAULT_REVIEW_POLICY,
    Agreement,
    ModelComparison,
    ReviewOutcome,
    ReviewPolicy,
    compare_battery_models,
)
from .eligibility import (
    AircraftEnergyState,
    CandidateAssessment,
    FleetAssessment,
    MissionShape,
    assess_candidate,
    assess_fleet,
)
from .errors import (
    EnergyError,
    EnergyInputError,
    NoProgressError,
    ProfileConflictError,
    RouteBlockedError,
    TrackHoldingError,
    UnknownProfileError,
)
from .feasibility import (
    TERMS_IN_ORDER,
    BlockedLeg,
    MarginReport,
    MissionEnergyRequest,
    RejectReason,
    TermAttribution,
    TermName,
    assess_mission,
    feasible_report,
    rank_reports,
)
from .geometry import (
    climb_and_descent_mm,
    course_of,
    distance_mm_of,
    leg_between,
    route_distance_mm,
    route_from_points,
)
from .model import (
    Leg,
    LegEnergy,
    Route,
    RouteEnergy,
    SpeedProfile,
    SpeedStep,
    fly_leg,
    fly_route,
)
from .numeric import (
    BP_PER_UNIT,
    CurvePoint,
    apply_bp,
    ceil_div,
    format_curve,
    interpolate_curve,
    mwh_to_wh,
    parse_curve,
)
from .profile import ILLUSTRATIVE_DISCLAIMER, BatteryProfileVersion
from .profiles import (
    AIRFRAME_X_SIM,
    AIRFRAME_Y_SIM,
    ILLUSTRATIVE_ONLY_FIELDS,
    PARAMETER_PROVENANCE,
    available_profiles,
    load_profile,
    publish_profile,
)
from .sensitivity import (
    DEFAULT_STEPS,
    SWEEP_ORDER,
    Knob,
    KnobResponse,
    SensitivityReport,
    perturb,
    sweep_sensitivity,
)
from .trig import normalize_mdeg
from .weather import (
    Freshness,
    WindSample,
    WindSnapshot,
    snapshot_from_rows,
)

__all__ = [
    "AIRFRAME_X_SIM",
    "AIRFRAME_Y_SIM",
    "BP_PER_UNIT",
    "DEFAULT_REVIEW_POLICY",
    "DEFAULT_STEPS",
    "ILLUSTRATIVE_DISCLAIMER",
    "ILLUSTRATIVE_ONLY_FIELDS",
    "PARAMETER_PROVENANCE",
    "SWEEP_ORDER",
    "TERMS_IN_ORDER",
    "Agreement",
    "AircraftEnergyState",
    "BatteryProfileVersion",
    "BlockedLeg",
    "CandidateAssessment",
    "CurvePoint",
    "EnergyError",
    "EnergyInputError",
    "FleetAssessment",
    "Freshness",
    "Knob",
    "KnobResponse",
    "Leg",
    "LegEnergy",
    "MarginReport",
    "MissionEnergyRequest",
    "MissionShape",
    "ModelComparison",
    "NoProgressError",
    "ProfileConflictError",
    "RejectReason",
    "ReviewOutcome",
    "ReviewPolicy",
    "Route",
    "RouteBlockedError",
    "RouteEnergy",
    "SensitivityReport",
    "SpeedProfile",
    "SpeedStep",
    "TermAttribution",
    "TermName",
    "TrackHoldingError",
    "UnknownProfileError",
    "WindSample",
    "WindSnapshot",
    "apply_bp",
    "assess_candidate",
    "assess_fleet",
    "assess_mission",
    "available_profiles",
    "ceil_div",
    "climb_and_descent_mm",
    "compare_battery_models",
    "course_of",
    "distance_mm_of",
    "feasible_report",
    "fly_leg",
    "fly_route",
    "format_curve",
    "interpolate_curve",
    "leg_between",
    "load_profile",
    "mwh_to_wh",
    "normalize_mdeg",
    "parse_curve",
    "perturb",
    "publish_profile",
    "rank_reports",
    "route_distance_mm",
    "route_from_points",
    "snapshot_from_rows",
    "sweep_sensitivity",
]
