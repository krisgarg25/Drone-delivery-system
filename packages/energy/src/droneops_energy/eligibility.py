"""Aircraft eligibility *inputs*, not eligibility decisions.

``packages/dispatcher`` owns assignment, queueing, priority and fairness, and it
is built next. This module is the query it calls: given a mission shape and a list
of aircraft states, return each one's signed energy margin and why it fails, so
dispatch can rank and choose. Nothing here orders work by urgency, reserves an
aircraft, or filters by maintenance or offline status - ``status`` is carried
through untouched precisely so that the dispatcher's own eligibility filter
(invariant I5) stays the single place that decision is made.

The one safety decision this layer does make is the energy decision itself, and it
makes it identically for every candidate: a plan whose complete mission - outbound,
handover, recovery, reserve - does not fit is never offered as feasible.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from .errors import EnergyError
from .feasibility import (
    MarginReport,
    MissionEnergyRequest,
    RejectReason,
    TermName,
    assess_mission,
)
from .model import Route, SpeedProfile
from .numeric import BP_PER_UNIT, require
from .profile import BatteryProfileVersion
from .weather import WindSnapshot


@dataclass(frozen=True)
class MissionShape:
    """The mission with the aircraft removed: what every candidate shares."""

    mission_id: str
    payload_g: int
    outbound: Route
    handover: Route
    recovery: Route | None
    weather: WindSnapshot
    clock_us: int = 0
    deadline_us: int | None = None
    speed: SpeedProfile = field(default_factory=SpeedProfile)
    requires_alternate: bool = False

    def __post_init__(self) -> None:
        require(self.mission_id != "", "a mission shape needs an id")
        require(self.payload_g >= 0, "payload mass cannot be negative")

    def request_for(self, aircraft: AircraftEnergyState) -> MissionEnergyRequest:
        """Bind one aircraft state into a complete feasibility request."""
        return MissionEnergyRequest(
            mission_id=self.mission_id,
            payload_g=self.payload_g,
            outbound=self.outbound,
            handover=self.handover,
            recovery=self.recovery,
            weather=self.weather,
            profile=aircraft.profile,
            charge_bp=aircraft.charge_bp,
            soh_bp=aircraft.soh_bp,
            speed=self.speed,
            clock_us=self.clock_us,
            deadline_us=self.deadline_us,
            requires_alternate=self.requires_alternate,
        )


@dataclass(frozen=True)
class AircraftEnergyState:
    """The battery facts dispatch supplies; profile choice stays with dispatch."""

    aircraft_id: str
    profile: BatteryProfileVersion
    charge_bp: int = BP_PER_UNIT
    soh_bp: int = BP_PER_UNIT
    status: str = "available"

    def __post_init__(self) -> None:
        require(self.aircraft_id != "", "an aircraft candidate needs an id")
        require(0 <= self.charge_bp <= BP_PER_UNIT, "charge must be in basis points")
        require(0 <= self.soh_bp <= BP_PER_UNIT, "state of health must be in basis points")


@dataclass(frozen=True)
class CandidateAssessment:
    """One aircraft's answer, plus the pass-through status dispatch filtered on."""

    aircraft_id: str
    status: str
    profile_id: str
    parameters_sha256: str
    feasible: bool
    margin_wh: int | None
    reserve_required_wh: int
    estimated_consumption_wh: int
    breaking_term: TermName | None
    reasons: tuple[RejectReason, ...]
    report: MarginReport | None
    note: str = ""

    @property
    def rank_key(self) -> tuple[int, int, str]:
        """Best margin first, unknowns last, then aircraft id for a total order."""
        if self.margin_wh is None:
            return (1, 0, self.aircraft_id)
        return (0, -self.margin_wh, self.aircraft_id)

    @property
    def disclaimer(self) -> str:
        """Every answer carries its basis, including a refusal."""
        return self.report.disclaimer if self.report else _CANDIDATE_DISCLAIMER


_CANDIDATE_DISCLAIMER = (
    "Energy basis is ILLUSTRATIVE: simulation profile, not vendor or field data. "
    "Simulation only. Not authorised for flight."
)


def assess_candidate(shape: MissionShape, aircraft: AircraftEnergyState) -> CandidateAssessment:
    """Score one aircraft against one complete mission, without raising.

    An aircraft that cannot lift the load is an infeasible candidate with a named
    reason, not an exception: a dispatch scan of a whole fleet must not abort on
    the first unsuitable airframe.
    """
    try:
        report = assess_mission(shape.request_for(aircraft))
    except EnergyError as error:
        return CandidateAssessment(
            aircraft_id=aircraft.aircraft_id,
            status=aircraft.status,
            profile_id=aircraft.profile.profile_id,
            parameters_sha256=aircraft.profile.parameters_sha256,
            feasible=False,
            margin_wh=None,
            reserve_required_wh=0,
            estimated_consumption_wh=0,
            breaking_term=TermName.OUTBOUND,
            reasons=(),
            report=None,
            note=str(error),
        )
    return CandidateAssessment(
        aircraft_id=aircraft.aircraft_id,
        status=aircraft.status,
        profile_id=report.profile.profile_id,
        parameters_sha256=report.profile_sha256,
        feasible=report.feasible,
        margin_wh=report.margin_wh,
        reserve_required_wh=report.reserve_required_wh,
        estimated_consumption_wh=report.estimated_consumption_wh,
        breaking_term=report.breaking_term,
        reasons=report.reasons,
        report=report,
    )


@dataclass(frozen=True)
class FleetAssessment:
    """Feasible candidates ranked best-margin first, and the rejects with reasons."""

    feasible: tuple[CandidateAssessment, ...]
    rejected: tuple[CandidateAssessment, ...]

    @property
    def evaluated(self) -> int:
        return len(self.feasible) + len(self.rejected)

    @property
    def best(self) -> CandidateAssessment | None:
        """The highest-margin aircraft, or ``None`` when nothing fits."""
        return self.feasible[0] if self.feasible else None

    def ranking(self) -> tuple[tuple[str, int | None, bool], ...]:
        """Every candidate in order, for a decision record or a console table."""
        return tuple(
            (row.aircraft_id, row.margin_wh, row.feasible)
            for row in (*self.feasible, *self.rejected)
        )


def assess_fleet(shape: MissionShape, candidates: Sequence[AircraftEnergyState]) -> FleetAssessment:
    """Answer the energy half of "which aircraft can realistically fly this?".

    Input order is irrelevant: the result is sorted by a total key, so two runs
    over the same fleet produce the same ranking (invariant I7).
    """
    rows = [assess_candidate(shape, aircraft) for aircraft in candidates]
    rows.sort(key=lambda row: row.rank_key)
    feasible = tuple(row for row in rows if row.feasible)
    rejected = tuple(row for row in rows if not row.feasible)
    return FleetAssessment(feasible=feasible, rejected=rejected)


__all__ = [
    "AircraftEnergyState",
    "CandidateAssessment",
    "FleetAssessment",
    "MissionShape",
    "assess_candidate",
    "assess_fleet",
]
