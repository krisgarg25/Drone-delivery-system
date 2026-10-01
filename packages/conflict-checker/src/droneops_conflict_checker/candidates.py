"""Which committed records a requested set has to be checked against - and why.

``technical-build/11`` section 7 makes the checker a *second opinion about a set*, and a
set is only meaningful if it is the right set. The ledger's own recheck reads holders by
resource identity (``<kind>:<identifier>``) because that is the cheapest decisive question:
same physical thing, overlapping minutes. For the interval view that is sufficient. For
this checker it is not: two missions one lane apart, on different corridor keys, hold no
resource key in common at all, so an identity-only view never pairs them and the pair is
never argued about. That is the exact case the independent checker exists for, and it is
why the expansion below lives here rather than in the caller's query.

Four rules decide whether a committed record joins the set under examination, and a
record joins if **any** of them fires:

``SHARED_KEY``
    the record and the subject claim the same physical resource identity. This is what
    step 8 already looks at, restated in the checker's own vocabulary so the checker does
    not inherit a "the ledger already narrowed it" assumption.
``ADJACENT_LANE``
    the two records claim corridor lanes related by the world's lane map
    (``EvaluationContext.adjacency`` - the corridor graph's adjacency, supplied as data).
    Lanes that are neighbours are flown together only under an allow rule, and the
    time-blind corridor inspection is the checker's own reason for that.
``ADJACENT_CELL``
    the two records claim volume cells that are neighbours in ``geom-core``'s own grid:
    the tokens are parsed back into cells and enumerated with ``Cell3D.neighbourhood``,
    so adjacency is the grid's definition (Chebyshev radius one at the same level and
    band) and not a distance guess made here.
``OVERLAPPING_VOLUME``
    the checker's map-free rule: swept on a frame built from the pair, the two plans'
    protected volumes reach each other while their padded windows and vertical extents
    overlap. Two lanes nobody declared adjacent, but whose volumes touch, still get
    checked - which is the whole point of not trusting a map.

Selection is a *wider question to ask*, never a verdict: everything it admits still goes
through :func:`~droneops_conflict_checker.checks.check_pair`, so a record admitted by
geometry and then cleared reports ``SAFE`` with a cause. The converse is what this module
prevents - a record never admitted because it shares no key, and therefore never answered
about at all.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal

from droneops_geom_core import Cell3D, GeomCoreError, GridSpec, Interval

from . import geometry
from .policy import CheckerPolicy
from .records import CommittedPlanRecord, EvaluationContext
from .sweep import PairFrame, SweepError, SweptPlan, sweep_plan

#: The four admission rules, in the order a report lists them.
AdjacencyRule = Literal["SHARED_KEY", "ADJACENT_LANE", "ADJACENT_CELL", "OVERLAPPING_VOLUME"]

RULES: Final[tuple[AdjacencyRule, ...]] = (
    "SHARED_KEY",
    "ADJACENT_LANE",
    "ADJACENT_CELL",
    "OVERLAPPING_VOLUME",
)

#: Chebyshev radius of the cell neighbourhood the checker admits: one ring, so a cell that
#: merely touches a reserved cell is checked and a cell two rings away is not.
DEFAULT_CELL_RADIUS: Final[int] = 1


@dataclass(frozen=True)
class CandidateSelection:
    """One committed record admitted to the set, with every rule that admitted it.

    The rules are reported, not just the boolean: when a release is refused because a pair
    was checked, the decision record has to say *why this pair was in the set at all*, and
    "it looked adjacent" is not an answer a reviewer can audit.
    """

    plan_version: str
    mission_id: str
    subject: str
    rules: tuple[AdjacencyRule, ...]
    detail: str

    def sort_key(self) -> tuple[str, str, str]:
        return (self.subject, self.plan_version, self.mission_id)

    def as_text(self) -> str:
        return (
            f"{self.subject} + {self.plan_version}: " + ",".join(self.rules) + f" ({self.detail})"
        )


@dataclass(frozen=True)
class Selection:
    """The whole answer: what joined the set, and how big the asking was."""

    included: tuple[CandidateSelection, ...]
    pool_size: int
    subject_count: int

    @property
    def records(self) -> tuple[str, ...]:
        """Plan versions admitted, in canonical order."""
        return tuple(item.plan_version for item in self.included)

    def reasons(self) -> tuple[str, ...]:
        return tuple(item.as_text() for item in self.included)


def resource_identities(record: CommittedPlanRecord) -> tuple[str, ...]:
    """``<kind>:<identifier>`` of everything this record claims, windowless and sorted.

    A non-canonical key yields an empty tuple rather than an exception: an unparseable key
    is a defect :func:`~droneops_conflict_checker.records.validate_record` already reports
    as ``UNKNOWN_RESOURCE_KEY``, and admission must not lose the geometric rules because a
    key is malformed.
    """
    try:
        return record.all_identifiers()
    except GeomCoreError:
        return ()


def lane_keys(record: CommittedPlanRecord) -> tuple[str, ...]:
    """The corridor lane identifiers claimed, or nothing when the keys are broken."""
    try:
        return record.corridor_identifiers()
    except GeomCoreError:
        return ()


def cell_tokens(record: CommittedPlanRecord) -> tuple[str, ...]:
    """The volume-cell qualifiers this record claims, sorted and deduplicated."""
    found: set[str] = set()
    for raw in record.resource_keys:
        kind, _, rest = raw.partition(":")
        if kind.strip().lower() != "volume_cell":
            continue
        qualifier = rest.partition(":")[0].strip()
        if qualifier:
            found.add(qualifier)
    return tuple(sorted(found))


def lanes_related(left: str, right: str, context: EvaluationContext) -> bool:
    """Whether two lanes are neighbours in the world's lane map, in either direction.

    Both directions are asked rather than assumed: a lane map that lists ``B`` under ``A``
    but forgot to list ``A`` under ``B`` still admits the pair, because adjacency is a
    property of the world and not of whichever half of the map was written.
    """
    if left == right:
        return True
    return right in context.neighbours_of(left) or left in context.neighbours_of(right)


def parsed_cells(
    record: CommittedPlanRecord, grid: GridSpec, context: EvaluationContext
) -> tuple[Cell3D, ...]:
    """This record's claimed cells as ``geom-core`` cells; tokens that are not cells drop out.

    A synthetic cell label is not a ``geohash3d`` token, and the answer is to hand the pair
    to the geometric rule rather than to invent a grid position the ledger never reserved.
    """
    reference = context.policy.altitude_reference
    found: list[Cell3D] = []
    for token in cell_tokens(record):
        try:
            found.append(Cell3D.parse_token(grid, token, reference=reference))
        except GeomCoreError:
            continue
    return tuple(found)


def cells_adjacent(
    left: CommittedPlanRecord,
    right: CommittedPlanRecord,
    context: EvaluationContext,
    *,
    radius: int = DEFAULT_CELL_RADIUS,
) -> bool:
    """``geom-core``'s own neighbourhood enumeration, applied to the two records' cells.

    Only meaningful when the run froze a grid: without one there is no cell size, and
    inventing a grid in order to enumerate neighbours would invent adjacency relations the
    ledger never reserved against.
    """
    if context.grid is None:
        return False
    left_cells = parsed_cells(left, context.grid, context)
    right_cells = parsed_cells(right, context.grid, context)
    if not left_cells or not right_cells:
        return False
    for cell in left_cells:
        try:
            ring = set(cell.neighbourhood(radius=radius))
        except GeomCoreError:
            # A cell at the grid domain edge has no complete ring. The cells that do exist
            # are still its neighbours, so the pair is admitted rather than refused.
            return True
        if any(other in ring for other in right_cells):
            return True
    return False


def windows_overlap(left: CommittedPlanRecord, right: CommittedPlanRecord) -> bool:
    """Absolute-time overlap of two records, decided without sweeping either.

    A record whose instants cannot be read is admitted rather than refused: no time bound
    is not the same statement as "no overlap", and the pair report answers ``UNPROVEN``.
    """
    first, second = left.window_us(), right.window_us()
    if first is None or second is None:
        return True
    return first[0] <= second[1] and second[0] <= first[1]


def volumes_reach(left: CommittedPlanRecord, right: CommittedPlanRecord, policy: CheckerPolicy) -> bool:
    """Whether the two plans' protected volumes touch at all, on the checker's own sweep.

    Deliberately the coarse question - plan-level polylines, whole vertical extents -
    because a near miss is still a pair worth asking about. The precise contact test is
    :func:`~droneops_conflict_checker.checks.check_pair`'s, and this cannot pre-empt it.
    """
    try:
        frame = PairFrame.of(left, right)
    except SweepError:
        # The pair cannot share a projected plane, which the pair report answers as
        # UNPROVEN. Admit the pair rather than hide the reason.
        return True
    span_left, span_right = _window(left), _window(right)
    if span_left is None or span_right is None or not span_left.overlaps(span_right):
        return False
    try:
        first = sweep_plan(left, frame, policy)
        second = sweep_plan(right, frame, policy)
    except SweepError:
        return True
    reach = _planar_reach(first) + _planar_reach(second)
    if geometry.polyline_gap_mm(first.float_track(), second.float_track()) > float(reach):
        return False
    low_left, high_left = _vertical_extent(first)
    low_right, high_right = _vertical_extent(second)
    return low_left < high_right and low_right < high_left


def _window(record: CommittedPlanRecord) -> Interval | None:
    """The record's own absolute span as a half-open interval, or ``None``."""
    span = record.window_us()
    return None if span is None else Interval.of(span[0], span[1] + 1)


def _planar_reach(plan: SweptPlan) -> int:
    """Widest lateral protection carried by any piece of a swept plan."""
    return max((piece.reach_mm for piece in plan.pieces), default=0)


def _vertical_extent(plan: SweptPlan) -> tuple[int, int]:
    """Lowest and highest protected height of a swept plan, half-open."""
    if not plan.pieces:
        return (0, 0)
    low = min(min(piece.alt_start, piece.alt_end) - piece.vertical_half_mm for piece in plan.pieces)
    high = max(max(piece.alt_start, piece.alt_end) + piece.vertical_half_mm for piece in plan.pieces)
    return int(low), int(high) + 1


def _rules_for(
    subject: CommittedPlanRecord,
    record: CommittedPlanRecord,
    context: EvaluationContext,
    *,
    cell_radius: int,
) -> tuple[tuple[AdjacencyRule, ...], str]:
    """Every rule that admits this pair, plus one line saying what actually fired.

    Time gates all four rules: two plans that can never be airborne in the same minutes are
    not neighbours in the sense this module cares about, and no inspection below can find a
    contact between them. A record whose instants cannot be read at all stays admitted,
    because an unknown window is not a disjoint one.
    """
    if not windows_overlap(subject, record):
        return ((), "")
    fired: list[AdjacencyRule] = []
    notes: list[str] = []
    shared = sorted(set(resource_identities(subject)) & set(resource_identities(record)))
    if shared:
        fired.append("SHARED_KEY")
        notes.append("identity " + ",".join(shared))
    lanes = sorted(
        f"{left}~{right}"
        for left in lane_keys(subject)
        for right in lane_keys(record)
        if lanes_related(left, right, context)
    )
    if lanes:
        fired.append("ADJACENT_LANE")
        notes.append("lanes " + ", ".join(lanes))
    if cell_tokens(subject) and cell_tokens(record) and cells_adjacent(
        subject, record, context, radius=cell_radius
    ):
        fired.append("ADJACENT_CELL")
        notes.append(f"cells within radius {cell_radius}")
    if volumes_reach(subject, record, context.policy):
        fired.append("OVERLAPPING_VOLUME")
        notes.append("protected volumes reach")
    return (tuple(rule for rule in RULES if rule in fired), "; ".join(notes))


def select_adjacent_candidates(
    subjects: Sequence[CommittedPlanRecord],
    pool: Sequence[CommittedPlanRecord],
    context: EvaluationContext,
    *,
    cell_radius: int = DEFAULT_CELL_RADIUS,
) -> Selection:
    """Expand a requested set with the committed records that are *near* it, not just in it.

    A pool record is admitted when it holds airspace, is not itself one of the subjects, and
    does not belong to one of the subject missions - a mission's second resource is not
    evidence about itself, it is the same commitment - and when at least one rule fires. The
    answer is canonically ordered, so two runs over the same rows admit the same records in
    the same order.
    """
    holding_subjects = tuple(record for record in subjects if record.holds_airspace)
    named = {(record.plan_version, record.mission_id) for record in holding_subjects}
    missions = {record.mission_id for record in holding_subjects if record.mission_id}
    ordered_subjects = tuple(
        sorted(holding_subjects, key=lambda record: (record.plan_version, record.mission_id))
    )
    included: list[CandidateSelection] = []
    for record in sorted(
        (item for item in pool if item.holds_airspace),
        key=lambda item: (item.plan_version, item.mission_id),
    ):
        if (record.plan_version, record.mission_id) in named or record.mission_id in missions:
            continue
        for subject in ordered_subjects:
            rules, detail = _rules_for(subject, record, context, cell_radius=cell_radius)
            if rules:
                included.append(
                    CandidateSelection(
                        plan_version=record.plan_version,
                        mission_id=record.mission_id,
                        subject=subject.plan_version,
                        rules=rules,
                        detail=detail,
                    )
                )
                break
    return Selection(
        included=tuple(sorted(included, key=CandidateSelection.sort_key)),
        pool_size=len(pool),
        subject_count=len(ordered_subjects),
    )


def admit_records(
    subjects: Sequence[CommittedPlanRecord],
    pool: Sequence[CommittedPlanRecord],
    context: EvaluationContext,
    *,
    cell_radius: int = DEFAULT_CELL_RADIUS,
) -> tuple[CommittedPlanRecord, ...]:
    """The pool records the selection admitted, canonical-ordered and deduplicated."""
    selection = select_adjacent_candidates(subjects, pool, context, cell_radius=cell_radius)
    admitted = {item.plan_version for item in selection.included}
    return tuple(
        sorted(
            (record for record in pool if record.holds_airspace and record.plan_version in admitted),
            key=lambda record: (record.plan_version, record.mission_id),
        )
    )


__all__ = [
    "DEFAULT_CELL_RADIUS",
    "RULES",
    "AdjacencyRule",
    "CandidateSelection",
    "Selection",
    "admit_records",
    "cell_tokens",
    "cells_adjacent",
    "lane_keys",
    "lanes_related",
    "parsed_cells",
    "resource_identities",
    "select_adjacent_candidates",
    "volumes_reach",
    "windows_overlap",
]
