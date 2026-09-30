"""The three-way answer and everything that has to travel with it.

``SAFE | CONFLICT | UNPROVEN`` is the vocabulary the stub shipped with and the
vocabulary the invariant is stated in (``technical-build/06`` section 2, I1).
The type stays a ``Literal`` so a caller can compare against a string literal
without importing an enum, and so the legacy ``check_interval_overlap`` keeps
working unchanged.

What is added here is the *company* a verdict keeps:

* a verdict is never bare. ``UNPROVEN`` carries at least one
  :class:`UnprovenReason` - the reason is mandatory, and
  :attr:`PairVerdict.is_well_formed` is the property the test suite asserts,
  because an unprovable answer with no explanation is indistinguishable from a
  bug;
* ``CONFLICT`` carries at least one :class:`Contact`: when, where, which of the
  four inspections produced it, and how close the two bodies actually came;
* ``SAFE`` carries the *cause* that cleared it, so a reviewer can see whether it
  was cleared by time, by height, by lateral distance, or by having claimed no
  shared corridor at all.

Verdicts combine by worst case, never by majority and never by averaging:
``CONFLICT`` outranks ``UNPROVEN`` outranks ``SAFE``. That ordering *is* the
over-approximation posture written down - a proven contact is more actionable
than an unprovable question, and both block release while only ``SAFE`` does not.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Literal

Verdict = Literal["SAFE", "CONFLICT", "UNPROVEN"]

VERDICTS: Final[tuple[Verdict, ...]] = ("SAFE", "CONFLICT", "UNPROVEN")

_SAFE: Final[Verdict] = "SAFE"
_CONFLICT: Final[Verdict] = "CONFLICT"
_UNPROVEN: Final[Verdict] = "UNPROVEN"

#: Worst-case rank. Higher blocks harder.
_VERDICT_RANK: Final[dict[Verdict, int]] = {_SAFE: 0, _UNPROVEN: 1, _CONFLICT: 2}


class ReasonCode(StrEnum):
    """Why the checker could not reason about something. Never a warning."""

    MISSING_ALTITUDE_REFERENCE = "MISSING_ALTITUDE_REFERENCE"
    CROSS_DATUM_COMPARISON = "CROSS_DATUM_COMPARISON"
    ABSENT_PROVENANCE = "ABSENT_PROVENANCE"
    EXPIRED_RESTRICTION_DATA = "EXPIRED_RESTRICTION_DATA"
    STALE_TELEMETRY = "STALE_TELEMETRY"
    MALFORMED_PROFILE = "MALFORMED_PROFILE"
    NON_MONOTONIC_TIME = "NON_MONOTONIC_TIME"
    NAIVE_ABSOLUTE_TIME = "NAIVE_ABSOLUTE_TIME"
    NOT_A_COMMITTED_RECORD = "NOT_A_COMMITTED_RECORD"
    PLANAR_EXTENT_EXCEEDED = "PLANAR_EXTENT_EXCEEDED"
    POLICY_RELEASE_MISMATCH = "POLICY_RELEASE_MISMATCH"
    UNSUPPORTED_POLICY_SCHEMA = "UNSUPPORTED_POLICY_SCHEMA"
    UNKNOWN_RESOURCE_KEY = "UNKNOWN_RESOURCE_KEY"

    @property
    def human(self) -> str:
        """What a console or a trace says, in the operator's own language."""
        return _REASON_TEXT[self]


_REASON_TEXT: Final[dict[ReasonCode, str]] = {
    ReasonCode.MISSING_ALTITUDE_REFERENCE: (
        "a plan point carries no altitude reference, so its height cannot be compared with "
        "anything (invariant I9)"
    ),
    ReasonCode.CROSS_DATUM_COMPARISON: (
        "the two plans state different altitude references; converting between them needs site "
        "datums the record does not carry, so no clearance can be claimed"
    ),
    ReasonCode.ABSENT_PROVENANCE: (
        "the record has no plan version, no claimed resources or no recorded planner claim, so "
        "there is nothing to hold up as committed"
    ),
    ReasonCode.EXPIRED_RESTRICTION_DATA: (
        "the restriction or airspace data behind this plan is past its expiry, so the space it "
        "claims is no longer described by a current source"
    ),
    ReasonCode.STALE_TELEMETRY: (
        "the last position report is older than the freshness budget; a stale aircraft does not "
        "release the airspace it was protecting (invariant I3)"
    ),
    ReasonCode.MALFORMED_PROFILE: (
        "the profile cannot be swept at all: no points, a non-positive body extent, an "
        "unreadable coordinate or a reversed interval"
    ),
    ReasonCode.NON_MONOTONIC_TIME: (
        "plan times repeat or run backwards, so interpolating between them would invent a place "
        "the aircraft never was"
    ),
    ReasonCode.NAIVE_ABSOLUTE_TIME: (
        "a timestamp has no UTC offset; absolute-time reasoning needs the instant, not a "
        "wall-clock guess"
    ),
    ReasonCode.NOT_A_COMMITTED_RECORD: (
        "the record is not in a committed or activated state, so it is not part of the set the "
        "invariant is about"
    ),
    ReasonCode.PLANAR_EXTENT_EXCEEDED: (
        "the two plans are too far apart to share one projected frame, so their millimetres are "
        "not comparable"
    ),
    ReasonCode.POLICY_RELEASE_MISMATCH: (
        "a plan was built under a different protected-volume policy release than the active one; "
        "margins do not transfer between releases"
    ),
    ReasonCode.UNSUPPORTED_POLICY_SCHEMA: (
        "the active policy file declares a schema version this checker does not speak"
    ),
    ReasonCode.UNKNOWN_RESOURCE_KEY: (
        "a claimed resource key is not canonical, so the corridor and adjacency inspection "
        "cannot say what was claimed"
    ),
}

#: States whose records hold airspace and take part in the committed-set invariant.
COMMITTED_STATES: Final[frozenset[str]] = frozenset({"committed", "activated"})

#: States that are legitimately present in a ledger but claim no space.
NON_HOLDING_STATES: Final[frozenset[str]] = frozenset({"cancelled", "completed"})


@dataclass(frozen=True)
class UnprovenReason:
    """One thing the checker refused to guess about."""

    code: ReasonCode
    detail: str
    plan_version: str

    def sort_key(self) -> tuple[str, str, str]:
        """Documented order: code, then plan version, then detail."""
        return (self.code.value, self.plan_version, self.detail)

    def as_text(self) -> str:
        return f"{self.code.value}: {self.detail} [{self.plan_version or '-'}]"


class CheckName(StrEnum):
    """Which inspection produced a finding, so a trace can re-run that one alone."""

    CONTINUOUS_SWEEP = "CONTINUOUS_SWEEP"
    TRANSITION_BUFFER = "TRANSITION_BUFFER"
    ADJACENT_CORRIDOR = "ADJACENT_CORRIDOR"
    CROSSING_VOLUME = "CROSSING_VOLUME"
    FRAME_SAMPLE = "FRAME_SAMPLE"

    @property
    def question(self) -> str:
        """What this inspection actually asked, in one line."""
        return _CHECK_QUESTION[self]


_CHECK_QUESTION: Final[dict[CheckName, str]] = {
    CheckName.CONTINUOUS_SWEEP: (
        "do the two bodies' protected volumes cover one place at one instant, sweeping "
        "every instant between the plan points?"
    ),
    CheckName.TRANSITION_BUFFER: (
        "does either plan's manoeuvre and lateness slack, padded on both ends of every leg, "
        "reach into the other plan's protected volume?"
    ),
    CheckName.ADJACENT_CORRIDOR: (
        "do these two claimed lanes - the same lane, or lanes declared adjacent - come within "
        "each other's protection at all, ignoring where along them the aircraft were?"
    ),
    CheckName.CROSSING_VOLUME: (
        "do the two horizontal tracks cross, and do both plans arrive at that shared point "
        "inside the timing and transition tolerance?"
    ),
    CheckName.FRAME_SAMPLE: (
        "at each sampled instant, with nothing in between, do the two protected discs touch? "
        "This is the answer a 1 Hz monitor gives, and it is here for contrast only."
    ),
}


@dataclass(frozen=True)
class Contact:
    """Proof that two protected volumes covered the same place at one instant.

    Gaps are floats because they come out of a shapely computation, and they are
    always rounded *outward*: a gap reported as ``3.2`` mm may be 3.21 mm but is
    never 3.19 mm. A contact therefore never understates how close the bodies
    were, which is the direction that matters.
    """

    at_us: int
    window_start_us: int
    window_end_us: int
    east_mm: int
    north_mm: int
    altitude_mm: int
    horizontal_gap_mm: float
    vertical_gap_mm: float
    check: CheckName
    #: The two leg indices that met, ascending. Sorted on purpose: a contact is a fact
    #: about a *pair* of legs, and storing it in argument order would make the verdict
    #: of (A, B) differ from the verdict of (B, A) about the same event in the same
    #: window. Which plan owns which leg comes from ``subject`` and the records, not
    #: from this field, and the symmetry property test is what keeps that honest.
    legs: tuple[int, int]

    def __post_init__(self) -> None:
        object.__setattr__(self, "legs", tuple(sorted(self.legs)))

    def sort_key(self) -> tuple[int, str, int, int]:
        return (self.at_us, self.check.value, self.legs[0], self.legs[1])

    @property
    def span_us(self) -> int:
        """Whole instants this contact claim covers, always at least one."""
        return max(1, self.window_end_us - self.window_start_us)


@dataclass(frozen=True)
class CrossingFinding:
    """Where two tracks cross in the horizontal plane and when each body arrives.

    Arrival times are *clamped*: a body whose track does not reach the crossing
    point within its own window is reported as arriving at the window edge, so a
    crossing finding never claims a time the plan does not contain.
    """

    east_mm: int
    north_mm: int
    left_arrival_us: int
    right_arrival_us: int
    arrival_gap_us: int
    tolerance_us: int

    @property
    def uncontested(self) -> bool:
        """True when both bodies reach the shared point inside the tolerance."""
        return self.arrival_gap_us <= self.tolerance_us

    def sort_key(self) -> tuple[int, int]:
        return (self.east_mm, self.north_mm)


@dataclass(frozen=True)
class AdjacencyFinding:
    """Two claimed corridors that are the same lane, or lanes declared adjacent.

    ``planar_gap_mm`` is the minimum distance between the two *track polylines*,
    ignoring where along each track each aircraft is. That is deliberately worse
    resolution than the sweep, and it is what makes sharing an adjacent corridor
    need an allow rule rather than lucky timing.
    """

    left_key: str
    right_key: str
    relation: str
    planar_gap_mm: float
    windows_overlap: bool
    bands_overlap: bool
    reach_mm: int

    @property
    def contested(self) -> bool:
        """Related lanes, overlapping in absolute time and in height, within reach."""
        return (
            self.planar_gap_mm <= float(self.reach_mm)
            and self.windows_overlap
            and self.bands_overlap
        )

    def sort_key(self) -> tuple[str, str]:
        return (self.left_key, self.right_key)


@dataclass(frozen=True)
class CheckerMetrics:
    """Deterministic counts describing what a verdict cost to reach.

    Every field is a count or an outward-rounded distance over one fixed pair, so
    the same two records give the same metrics on every replay (I7).
    """

    legs_compared: int
    piece_pairs_tested: int
    contact_windows: int
    contact_span_us: int
    crossing_points: int
    corridor_pairs: int
    min_horizontal_gap_mm: float
    min_vertical_gap_mm: float
    unproven_reasons: int

    def as_dict(self) -> dict[str, float]:
        """JSON-ready form with fixed keys, so a golden bundle can name its expectations."""
        return {
            "legs_compared": float(self.legs_compared),
            "piece_pairs_tested": float(self.piece_pairs_tested),
            "contact_windows": float(self.contact_windows),
            "contact_span_us": float(self.contact_span_us),
            "crossing_points": float(self.crossing_points),
            "corridor_pairs": float(self.corridor_pairs),
            "min_horizontal_gap_mm": self.min_horizontal_gap_mm,
            "min_vertical_gap_mm": self.min_vertical_gap_mm,
            "unproven_reasons": float(self.unproven_reasons),
        }

    @classmethod
    def empty(cls) -> CheckerMetrics:
        """The metrics of a verdict that never reached geometry."""
        return cls(
            legs_compared=0,
            piece_pairs_tested=0,
            contact_windows=0,
            contact_span_us=0,
            crossing_points=0,
            corridor_pairs=0,
            min_horizontal_gap_mm=_NOT_MEASURED,
            min_vertical_gap_mm=_NOT_MEASURED,
            unproven_reasons=0,
        )


#: Sentinel for "this pair never got far enough to measure a gap".
_NOT_MEASURED: Final[float] = 1.0e30


@dataclass(frozen=True)
class AllowGrant:
    """A policy exception *both* sides agreed to, recorded with its geometric envelope.

    ``named_by`` is the pair of plan versions that each named the rule, which is
    what makes the agreement auditable rather than assumed: a rule that only one
    side of the pair references has not been agreed, it has been asserted.
    """

    rule_id: str
    rule_version: str
    policy_version: str
    named_by: tuple[str, str]
    measured_contact_span_us: int
    max_contact_span_us: int
    measured_min_separation_mm: float
    floor_mm: int

    @property
    def within_envelope(self) -> bool:
        """A rule's geometric floor is never waived by a rule.

        Both limits are checked against what the checker *measured*, not against
        what the planner claimed: an allow rule says "this kind of interaction is
        acceptable up to here", and if the actual contact is longer or closer than
        here, the exception does not cover it and the conflict stands.
        """
        return (
            self.measured_contact_span_us <= self.max_contact_span_us
            and self.measured_min_separation_mm >= float(self.floor_mm)
        )


@dataclass(frozen=True)
class PairVerdict:
    """The checker's complete answer about one pair of committed plans."""

    subject: str
    verdict: Verdict
    cause: str
    reasons: tuple[UnprovenReason, ...]
    contacts: tuple[Contact, ...]
    crossings: tuple[CrossingFinding, ...]
    corridors: tuple[AdjacencyFinding, ...]
    metrics: CheckerMetrics
    grant: AllowGrant | None = None
    contested_cell: str | None = None

    @property
    def blocks_release(self) -> bool:
        """Anything that is not a clean SAFE stops the release."""
        return self.verdict != _SAFE

    @property
    def is_well_formed(self) -> bool:
        """``UNPROVEN`` always carries a reason, ``CONFLICT`` always carries a contact.

        A granted conflict is well formed only while the grant still covers it; if
        the measured contact escapes the rule's envelope the grant is void and this
        says so, rather than quietly reporting the exception as a clearance.
        """
        if self.verdict == _UNPROVEN:
            return bool(self.reasons)
        if self.verdict == _CONFLICT:
            if not self.contacts:
                return False
            return self.grant is None or self.grant.within_envelope
        return self.verdict == _SAFE

    @property
    def first_contact_us(self) -> int | None:
        if not self.contacts:
            return None
        return min(contact.at_us for contact in self.contacts)

    def reason_text(self) -> tuple[str, ...]:
        """Sorted human sentences, for a console or a decision trace."""
        return tuple(
            reason.as_text() for reason in sorted(self.reasons, key=UnprovenReason.sort_key)
        )

    def explain(self) -> str:
        """One line that says what happened and why."""
        if self.verdict == _SAFE:
            return f"{self.subject}: SAFE ({self.cause})"
        if self.verdict == _CONFLICT:
            first = self.contacts[0]
            return (
                f"{self.subject}: CONFLICT via {first.check.value} at {first.at_us} us, "
                f"{len(self.contacts)} contact(s)"
            )
        return f"{self.subject}: UNPROVEN - " + "; ".join(self.reason_text())


def worst(candidates: Iterable[Verdict]) -> Verdict:
    """The strongest verdict in ``candidates``: CONFLICT, else UNPROVEN, else SAFE.

    An empty collection yields ``SAFE``, which no caller in this package is
    allowed to use as an excuse: a pair the checker could not reason about
    accumulates a reason before it accumulates a verdict.
    """
    ranked = sorted(candidates, key=lambda item: _VERDICT_RANK[item], reverse=True)
    return ranked[0] if ranked else _SAFE
