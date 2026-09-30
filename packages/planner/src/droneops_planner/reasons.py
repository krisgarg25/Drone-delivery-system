"""Planner errors and the reason-code table.

Rejection is a first-class result (``docs/conventions.md`` section 11), so every
reason carries a human sentence and a technical detail with the numbers that
produced it. A rejection with only a code is not a rejection the platform can
explain, and the pipeline refuses to emit one: :class:`Rejection` raises if either
field is empty.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .model_util import canonical_digest


class PlannerError(Exception):
    """Base class for planner failures."""


class RequestValidationError(PlannerError):
    """The request itself is malformed; nothing was planned."""


class PipelineConfigError(PlannerError):
    """The pipeline is wired with a policy or port that cannot satisfy the contract."""


class ReasonCode(StrEnum):
    """Every way this pipeline can say no, named so a metric can count it."""

    ORIGIN_UNKNOWN = "origin_unknown"
    DESTINATION_UNKNOWN = "destination_unknown"
    ORIGIN_EQUALS_DESTINATION = "origin_equals_destination"
    DEADLINE_BEFORE_ACCEPTANCE = "deadline_before_acceptance"
    HORIZON_CLOSED = "horizon_closed"
    OUTSIDE_OPERATING_WINDOW = "outside_operating_window"
    AIRCRAFT_INELIGIBLE = "aircraft_ineligible"
    PAYLOAD_EXCEEDS_ENVELOPE = "payload_exceeds_envelope"
    NO_ROUTE_FOUND = "no_route_found"
    CANDIDATE_LIMIT_TOO_SMALL = "candidate_limit_too_small"
    LANE_CLOSED = "lane_closed"
    NO_SAFE_WINDOW = "no_safe_window"
    VOLUME_CONFLICT = "volume_conflict"
    PAD_WINDOW_UNAVAILABLE = "pad_window_unavailable"
    CHARGER_UNAVAILABLE = "charger_unavailable"
    CREW_UNAVAILABLE = "crew_unavailable"
    ENERGY_BELOW_RESERVE = "energy_below_reserve"
    WEATHER_EXCEEDS_LANE_CAP = "weather_exceeds_lane_cap"
    DEADLINE_MISSED = "deadline_missed"
    COMMIT_STALE_VERSION = "commit_stale_version"
    COMMIT_CONFLICT = "commit_conflict"
    RETRY_LIMIT_EXCEEDED = "retry_limit_exceeded"
    UNPROVEN_DEPENDENCY = "unproven_dependency"


_REASONS: dict[ReasonCode, tuple[str, str]] = {
    ReasonCode.ORIGIN_UNKNOWN: (
        "The pickup site is not part of this lane network.",
        "origin node id {detail} is absent from graph {graph_version}",
    ),
    ReasonCode.DESTINATION_UNKNOWN: (
        "The drop-off site is not part of this lane network.",
        "destination node id {detail} is absent from graph {graph_version}",
    ),
    ReasonCode.ORIGIN_EQUALS_DESTINATION: (
        "The pickup and drop-off sites are the same pad.",
        "origin and destination both {detail}",
    ),
    ReasonCode.DEADLINE_BEFORE_ACCEPTANCE: (
        "The order's deadline is earlier than the time it was accepted.",
        "deadline_us {detail} precedes requested_at_us",
    ),
    ReasonCode.HORIZON_CLOSED: (
        "There is no planning window left before the scenario cutoff.",
        "horizon [{detail}) is empty or outside the frozen scenario",
    ),
    ReasonCode.OUTSIDE_OPERATING_WINDOW: (
        "The site is closed at the time this mission would need it.",
        "facility window {detail}",
    ),
    ReasonCode.AIRCRAFT_INELIGIBLE: (
        "This aircraft is not allowed to be assigned right now.",
        "aircraft {detail} is not in an assignable state",
    ),
    ReasonCode.PAYLOAD_EXCEEDS_ENVELOPE: (
        "The payload is heavier than this aircraft may carry.",
        "payload_g {detail} exceeds the envelope maximum",
    ),
    ReasonCode.NO_ROUTE_FOUND: (
        "No lane sequence connects the two sites under the active closures.",
        "{detail}",
    ),
    ReasonCode.CANDIDATE_LIMIT_TOO_SMALL: (
        "The policy asked for fewer candidates than the minimum this build compares.",
        "requested k {detail} is below k_min",
    ),
    ReasonCode.LANE_CLOSED: (
        "A lane this route needs is closed for the whole trip.",
        "{detail}",
    ),
    ReasonCode.NO_SAFE_WINDOW: (
        "Every entry time on this route is blocked by committed traffic.",
        "{detail}",
    ),
    ReasonCode.VOLUME_CONFLICT: (
        "This route's protected volume overlaps a committed reservation.",
        "{detail}",
    ),
    ReasonCode.PAD_WINDOW_UNAVAILABLE: (
        "No departure or arrival pad is free in the needed window.",
        "{detail}",
    ),
    ReasonCode.CHARGER_UNAVAILABLE: (
        "The aircraft cannot be charged in time for this mission.",
        "{detail}",
    ),
    ReasonCode.CREW_UNAVAILABLE: (
        "No crew shift covers the turnaround.",
        "{detail}",
    ),
    ReasonCode.ENERGY_BELOW_RESERVE: (
        "This route would land with less charge than the reserve policy allows.",
        "{detail}",
    ),
    ReasonCode.WEATHER_EXCEEDS_LANE_CAP: (
        "The frozen wind trace is above what this lane declares it can fly in.",
        "{detail}",
    ),
    ReasonCode.DEADLINE_MISSED: (
        "This route cannot arrive before the order's deadline.",
        "{detail}",
    ),
    ReasonCode.COMMIT_STALE_VERSION: (
        "Traffic changed while this plan was being prepared, so it was recomputed.",
        "ledger version {detail}",
    ),
    ReasonCode.COMMIT_CONFLICT: (
        "The reservation ledger refused this plan.",
        "{detail}",
    ),
    ReasonCode.RETRY_LIMIT_EXCEEDED: (
        "Contention did not settle within the bounded recompute allowance.",
        "{detail}",
    ),
    ReasonCode.UNPROVEN_DEPENDENCY: (
        "A required evaluation could not be completed, so nothing was proposed.",
        "{detail}",
    ),
}


def human_reason(code: ReasonCode) -> str:
    """The operator-facing sentence for a reason code."""
    return _REASONS[code][0]


def technical_template(code: ReasonCode) -> str:
    """The audit-facing template, filled with the numbers that produced the code."""
    return _REASONS[code][1]


@dataclass(frozen=True)
class Rejection:
    """One rejected candidate, with both explanations and the evidence."""

    subject: str
    code: ReasonCode
    detail: str
    graph_version: str = ""
    evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.detail:
            raise PlannerError(
                f"rejection {self.code.value} for {self.subject!r} must carry technical detail"
            )
        if not self.subject:
            raise PlannerError("a rejection must name what it rejected")

    @property
    def human_reason(self) -> str:
        return human_reason(self.code)

    @property
    def technical_detail(self) -> str:
        return technical_template(self.code).format(
            detail=self.detail, graph_version=self.graph_version or "unversioned"
        )

    def as_row(self) -> tuple[str, str, str, str]:
        """``(subject, code, human, technical)`` - the shape the console renders."""
        return (self.subject, self.code.value, self.human_reason, self.technical_detail)

    def digest(self) -> str:
        return canonical_digest(
            [self.subject, self.code.value, self.detail, self.evidence]
        )


__all__ = [
    "PlannerError",
    "PipelineConfigError",
    "ReasonCode",
    "Rejection",
    "RequestValidationError",
    "human_reason",
    "technical_template",
]
