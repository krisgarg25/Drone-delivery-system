"""The independent adversarial conflict checker - the reason a reservation claim means anything.

Read ``docs/conventions.md`` section 6 and ``technical-build/11`` section 7 before
touching this package. Both say the same thing in different words: the planner and
the checker are **two implementations on purpose**, and the checker's job is to be a
second opinion, not a second pass.

Why the duplication is deliberate
    The planner answers a scheduling question fast. It sweeps protected volumes built
    from its own route profiles, inflates them with its own policy object, indexes them
    onto ``geom-core`` cells, and decides contacts with closed-form rational arithmetic
    over prisms. Every one of those choices is right for a planner and every one of them
    is a *single* choice: if the checker reused the same builder, the same margins object
    and the same intersection kernel, then "planner and checker agree" would be a
    tautology. It would be one implementation answering twice, and a bug in it - a
    sign error in a buffer, a margin added on one axis and not the other, a bucket that
    rounds a leg short - would be invisible precisely because both sides of the argument
    shared it. Agreement is only evidence when the two parties could have disagreed.

    So this package re-derives the protected volumes from the raw plan points in the
    committed record, projects them on a frame chosen from the pair rather than from
    either plan, and decides contacts by asking shapely how far a *relative* track sits
    from the origin, with the absolute-time window arithmetic done on ``geom-core``
    half-open microsecond intervals. Different library, different construction,
    different arithmetic, and no import of ``droneops_planner`` or ``droneops_geom_4d`` -
    an ``.importlinter`` contract keeps it that way and this package's own test suite
    re-checks the source text, because a contract that only exists in a config file is a
    comment.

What would be lost by sharing code
    A shared builder would make the checker's verdict inherit the planner's bugs; a
    shared policy object would make the checker's margins inherit the planner's units
    and rounding; a shared intersection kernel would turn every property test in this
    package into a check that a function equals itself. The cost of the duplication is
    that the checker is slower and says ``CONFLICT`` about things the planner waved
    through. That cost is the deliverable: a validator that never disagrees with the
    thing it validates has no information to contribute.

The three answers, and what each one does
    ``SAFE``
        every inspection cleared the pair, under the active policy release, on inputs
        the checker could reason about.
    ``CONFLICT``
        at least one proven contact, with the instant, the position, the closest
        approach and the inspection that found it.
    ``UNPROVEN``
        the checker could not answer, always with a reason: a missing altitude
        reference, a cross-datum comparison, absent provenance, expired restriction
        data, a malformed profile, a naive timestamp, a plan built under a different
        policy release. ``UNPROVEN`` **blocks release** - unknown is never safe, and a
        validator that returned ``SAFE`` when confused would be a validator that fails
        open.

Conservatism, and its direction
    The checker prefers a false ``CONFLICT`` to a false ``SAFE`` everywhere the two are
    possible: the transition buffer pads *every* leg rather than only the vertical ones
    the planner pads; an adjacent-corridor inspection ignores where along each lane the
    aircraft actually is; a crossing inspection compares arrival times at plan level;
    every reported distance leaves the shapely float path rounded *up*. When planner and
    checker disagree, :class:`~droneops_conflict_checker.checks.Disagreement` records it
    and blocks the release, in both directions, and there is no code path here that
    reconciles a checker ``CONFLICT`` with a planner ``SAFE`` by preferring the planner.

The legacy interval check below stays exactly as the stub shipped it. It is what
``droneops_reservations.validate_commit_set`` is wired to, it is a deliberately simple
absolute-time answer, and it is *not* the checker this package is about: two reservations
whose times overlap is necessary but nowhere near sufficient, and the 4D sweep above is
what makes the invariant provable.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from .bundle import (
    BundleError,
    ScenarioBundle,
    ScenarioExpectation,
    bundle_from_document,
    bundles_in,
    load_bundle,
    record_from_mapping,
)
from .candidates import (
    DEFAULT_CELL_RADIUS,
    RULES,
    AdjacencyRule,
    CandidateSelection,
    Selection,
    admit_records,
    cell_tokens,
    cells_adjacent,
    lane_keys,
    lanes_related,
    parsed_cells,
    resource_identities,
    select_adjacent_candidates,
    volumes_reach,
    windows_overlap,
)
from .checks import (
    BASES,
    Admission,
    Basis,
    ConflictReport,
    Disagreement,
    Violation,
    check_pair,
    check_set,
    grant_for,
    pair_subject,
)
from .policy import (
    CONTACT_TOLERANCE_MM,
    DEFAULT_POLICY_RELPATH,
    SUPPORTED_SCHEMA_VERSION,
    AllowRule,
    AllowRuleSet,
    CheckerPolicy,
    PolicyInputError,
    allow_rule_from_mapping,
    allow_rules_from_yaml_text,
    load_policy_file,
    policy_from_mapping,
    policy_from_yaml_text,
)
from .records import (
    DEFAULT_TELEMETRY_BUDGET_US,
    CommittedPlanRecord,
    EvaluationContext,
    PlanPoint,
    PlannerClaim,
    absolute_us,
    altitude_reference_of,
    cell_token_of,
    utc_instant,
    validate_record,
)
from .sweep import (
    PairFrame,
    PieceKind,
    PositionPiece,
    SweptPlan,
    SweepError,
    TrackPoint,
    grid_step_us,
    pad_tail_us,
    sweep_plan,
)
from .verdict import (
    COMMITTED_STATES,
    VERDICTS,
    AdjacencyFinding,
    AllowGrant,
    CheckerMetrics,
    CheckName,
    Contact,
    CrossingFinding,
    PairVerdict,
    ReasonCode,
    UnprovenReason,
    Verdict,
    worst,
)

# ---------------------------------------------------------------- legacy stub, kept verbatim
VerdictLiteral = Verdict


def to_microseconds(value: datetime) -> int | None:
    """Legacy helper: an aware datetime to integer microseconds, ``None`` when naive."""
    if value.tzinfo is None:
        return None
    return int(value.timestamp() * 1_000_000)


def check_interval_overlap(
    left: tuple[datetime, datetime],
    right: tuple[datetime, datetime],
) -> Verdict:
    """Deliberately simple, absolute-time checker.

    This is not the planner. It uses half-open intervals and returns CONFLICT when two
    reservations intersect. A production checker will additionally inspect 4D geometry,
    uncertainty, transition buffers and policy exceptions from committed records.

    Kept as the entry point ``droneops_reservations.validate_commit_set`` is wired to.
    The 4D answer is :func:`check_pair` and :func:`check_set`; this function is the
    interval floor they are built on top of, and a pair that fails here fails for a
    reason the sweep cannot argue with.
    """
    try:
        l_start, l_end = map(to_microseconds, left)
        r_start, r_end = map(to_microseconds, right)
    except (TypeError, ValueError, OverflowError):
        return "UNPROVEN"

    if None in (l_start, l_end, r_start, r_end):
        return "UNPROVEN"
    if l_start is None or l_end is None or r_start is None or r_end is None:
        return "UNPROVEN"
    if l_end < l_start or r_end < r_start:
        return "UNPROVEN"
    if l_start < r_end and r_start < l_end:
        return "CONFLICT"
    return "SAFE"


__all__ = [
    # ---- verdict vocabulary
    "Verdict",
    "VerdictLiteral",
    "VERDICTS",
    "COMMITTED_STATES",
    "ReasonCode",
    "CheckName",
    "UnprovenReason",
    "Contact",
    "CrossingFinding",
    "AdjacencyFinding",
    "CheckerMetrics",
    "AllowGrant",
    "PairVerdict",
    "worst",
    # ---- policy and exceptions
    "CheckerPolicy",
    "PolicyInputError",
    "AllowRule",
    "AllowRuleSet",
    "CONTACT_TOLERANCE_MM",
    "DEFAULT_POLICY_RELPATH",
    "SUPPORTED_SCHEMA_VERSION",
    "policy_from_mapping",
    "policy_from_yaml_text",
    "load_policy_file",
    "allow_rule_from_mapping",
    "allow_rules_from_yaml_text",
    # ---- committed records
    "CommittedPlanRecord",
    "PlanPoint",
    "PlannerClaim",
    "EvaluationContext",
    "DEFAULT_TELEMETRY_BUDGET_US",
    "absolute_us",
    "utc_instant",
    "altitude_reference_of",
    "cell_token_of",
    "validate_record",
    # ---- the checker's own sweep
    "PairFrame",
    "TrackPoint",
    "PieceKind",
    "PositionPiece",
    "SweptPlan",
    "SweepError",
    "sweep_plan",
    "grid_step_us",
    "pad_tail_us",
    # ---- inspections, pair and batch
    "Basis",
    "BASES",
    "Admission",
    "check_pair",
    "check_set",
    "pair_subject",
    "grant_for",
    "Violation",
    "Disagreement",
    "ConflictReport",
    # ---- which records the set has to be checked against
    "AdjacencyRule",
    "RULES",
    "DEFAULT_CELL_RADIUS",
    "CandidateSelection",
    "Selection",
    "resource_identities",
    "lane_keys",
    "cell_tokens",
    "lanes_related",
    "parsed_cells",
    "cells_adjacent",
    "windows_overlap",
    "volumes_reach",
    "select_adjacent_candidates",
    "admit_records",
    # ---- golden scenario bundles
    "BundleError",
    "ScenarioBundle",
    "ScenarioExpectation",
    "bundle_from_document",
    "record_from_mapping",
    "load_bundle",
    "bundles_in",
    # ---- legacy interval floor
    "check_interval_overlap",
    "to_microseconds",
]
