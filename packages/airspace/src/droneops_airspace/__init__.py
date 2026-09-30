"""droneops_airspace: restriction validity and route eligibility.

WHAT THIS PACKAGE IS
    A simulator's answer to two questions. "Does this restriction apply?" - only when it
    is in force *and* the data supporting it is fresh; stale or expired data fails closed
    into a no-launch, never into a no-restriction. "Does this route meet it?" - intersect,
    blocked now, blocked during a stated window, or allowed with a named condition.
    It also owns the change rule: a new restriction invalidates unlaunched plans that
    intersect it and flags affected airborne missions, without ever dropping a commitment.

WHAT IT IS NOT
    A flight authorisation, clearance, permission, NOTAM or zone notification.

    # This is a simulation model. Nothing in this package is, or can produce, a legal
    # statement about Indian airspace: red/yellow zone *names* and altitude-band *shapes*
    # imitate published conventions so the simulation is legible to a reader, and
    # resembling a standard does not make it Indian legal alignment. No output here may
    # be shown to a pilot, a partner or a regulator as authority to fly.
    # DigitalSky / UTM data may enter this system only through `adapters/airspace-import`,
    # under authorised credentials, with source, fetch time and version recorded and with
    # conservative behaviour when it is stale (technical-build/01 section 16). This package
    # must never import that adapter, must never fetch anything, and must never be handed a
    # live feed of its own choosing: it evaluates the observations it is given. That is why
    # the import below is a boundary check in tests, not a stylistic preference.

CONTRACT WITH CONSUMERS
    ``planner`` and ``corridor-graph`` pass a route profile or an edge list plus a
    snapshot; they get a verdict, per-restriction reports and reasons. Every verdict field
    is frozen and every result carries ``authoritative=False`` plus the ``note`` above, so
    a decision record that quotes this package quotes it honestly. ``reservations`` decides
    commitments; this package only says whether airspace, as far as it is known, permits the
    question to be asked.
"""

from __future__ import annotations

from .errors import (
    AirspaceError,
    GeometryInputError,
    NotAFlightAuthorisationError,
    RestrictionInputError,
)
from .geometry import (
    Footprint,
    FootprintShape,
    comparable_footprint,
    shared_origin,
)
from .restriction import (
    AUTHORITATIVE_SOURCES,
    CONDITION_BY_KIND,
    EFFECT_BY_KIND,
    NOT_A_CLEARANCE,
    AltitudeLimit,
    FootprintSpec,
    GeoCoord,
    Restriction,
    RestrictionEffect,
    RestrictionKind,
)
from .snapshot import (
    NO_FEED_NOTE,
    NO_FEED_SOURCE,
    RestrictionSnapshot,
    SnapshotAssessment,
    SnapshotAvailability,
)
from .validity import (
    DataTrust,
    FreshnessPolicy,
    RestrictionValidity,
    TrustAssessment,
    WindowState,
    assess_trust,
    evaluate_restriction,
    window_state_at,
)
from .eligibility import (
    EligibilityResult,
    EligibilityVerdict,
    IntersectionReport,
    CorridorEdgeProfile,
    CorridorEligibility,
    RouteProfile,
    RouteWaypoint,
    evaluate_corridor_edges,
    evaluate_route,
)
from .change import (
    AffectedPlan,
    ChangeImpact,
    MissionPhase,
    PlanDisposition,
    PlanImpact,
    evaluate_new_restriction,
)

__all__ = [
    # honesty constants
    "NOT_A_CLEARANCE",
    "AUTHORITATIVE_SOURCES",
    "NO_FEED_NOTE",
    "NO_FEED_SOURCE",
    # errors
    "AirspaceError",
    "GeometryInputError",
    "RestrictionInputError",
    "NotAFlightAuthorisationError",
    # geometry
    "Footprint",
    "FootprintShape",
    "comparable_footprint",
    "shared_origin",
    # restriction model
    "RestrictionKind",
    "RestrictionEffect",
    "EFFECT_BY_KIND",
    "CONDITION_BY_KIND",
    "GeoCoord",
    "FootprintSpec",
    "AltitudeLimit",
    "Restriction",
    # validity
    "FreshnessPolicy",
    "DataTrust",
    "WindowState",
    "TrustAssessment",
    "RestrictionValidity",
    "assess_trust",
    "window_state_at",
    "evaluate_restriction",
    # snapshot
    "SnapshotAvailability",
    "SnapshotAssessment",
    "RestrictionSnapshot",
    # eligibility
    "EligibilityVerdict",
    "EligibilityResult",
    "IntersectionReport",
    "RouteWaypoint",
    "RouteProfile",
    "CorridorEdgeProfile",
    "CorridorEligibility",
    "evaluate_route",
    "evaluate_corridor_edges",
    # change handling
    "MissionPhase",
    "PlanDisposition",
    "AffectedPlan",
    "PlanImpact",
    "ChangeImpact",
    "evaluate_new_restriction",
]
