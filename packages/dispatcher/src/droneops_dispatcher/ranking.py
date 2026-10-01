"""Energy screen (I4), facility waits and the price of repositioning.

Three cost sources meet here and stay separate in the record:

* **Energy.** :func:`screen_fleet` asks ``droneops_energy`` for each candidate's
  signed margin against the *complete* mission - outbound, handover, recovery and
  reserve charged to one battery - and refuses a candidate whose margin is
  negative or whose recovery term broke. The dispatcher does not own that
  arithmetic and never re-implements it; it owns the decision not to rank anything
  the energy kernel did not offer, and it asserts exactly that
  (:class:`EnergyScreenResult`).
* **Facility waits.** Pad, crew and charger wait are counted apart
  (``pad_wait_us`` / ``crew_wait_us`` / ``charger_wait_us``) because "pad free but
  no pilot" and "pilot free but charger occupied" are different problems with
  different fixes; folding them into one "delay" number would hide which.
* **Repositioning.** ``droneops_facilities`` hands over alternate-site quotes with
  ``priced_by="dispatcher"`` and deliberately no cost. :func:`price_repositioning`
  supplies the cost - from the versioned airframe profile's own cruise power and
  reference airspeed - and refuses to invent a distance: a quote with no leg
  geometry stays incomparable rather than free.

Everything is integer units on one scale so the ranking total order cannot drift
with float rounding, and every row carries the policy versions that made it.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from droneops_energy import (
    AircraftEnergyState,
    CandidateAssessment,
    EnergyError,
    MissionShape,
    Route,
    WindSnapshot,
    assess_fleet,
    load_profile,
)
from droneops_facilities import AlternateSiteQuote, WaitEvent, WaitReason

from .eligibility import FleetAircraft
from .errors import DispatchInputError
from .policy import (
    MICROS_PER_HOUR,
    MICROS_PER_MINUTE,
    MICROS_PER_SECOND,
    DispatchPolicy,
    digest_of,
)

INCOMPARABLE_NO_GEOMETRY: str = "no_leg_geometry"
INCOMPARABLE_EXCLUDED: str = "site_excluded_by_facilities"
CLIMB_RATE_MM_PER_S: int = 2500
"""The declared synthetic climb rate used to price the vertical part of a leg."""


@dataclass(frozen=True)
class MissionLegs:
    """The mission's route shape in millimetres, without an aircraft attached."""

    mission_id: str
    payload_g: int
    outbound_mm: int
    handover_mm: int
    recovery_mm: int
    climb_mm: int = 0
    hover_us: int = 0

    def __post_init__(self) -> None:
        if not self.mission_id:
            raise DispatchInputError("mission legs need an id")
        if min(self.outbound_mm, self.handover_mm, self.recovery_mm) < 0:
            raise DispatchInputError("leg lengths cannot be negative")
        if self.outbound_mm <= 0:
            raise DispatchInputError(f"{self.mission_id}: an outbound leg of 0 mm is no mission")

    def to_shape(
        self, weather: WindSnapshot, *, clock_us: int, deadline_us: int | None
    ) -> MissionShape:
        """Bind the legs to the frozen weather the energy kernel charges against."""
        handover = (
            Route.straight("handover", self.handover_mm)
            if self.handover_mm > 0
            else (
                Route.idle("handover", hover_us=self.hover_us)
                if self.hover_us
                else Route("handover")
            )
        )
        return MissionShape(
            mission_id=self.mission_id,
            payload_g=self.payload_g,
            outbound=Route.straight("outbound", self.outbound_mm, climb_mm=self.climb_mm),
            handover=handover,
            recovery=Route.straight("recovery", self.recovery_mm, descent_mm=self.climb_mm),
            weather=weather,
            clock_us=clock_us,
            deadline_us=deadline_us,
        )


@dataclass(frozen=True)
class EnergyScreen:
    """One candidate's margin, exactly as the energy kernel reported it."""

    aircraft_id: str
    status: str
    feasible: bool
    margin_wh: int
    consumption_wh: int
    reserve_required_wh: int
    breaking_term: str
    reason_codes: tuple[str, ...]
    profile_id: str
    profile_version: str
    parameters_sha256: str
    disclaimer: str
    note: str = ""

    @property
    def clears_reserve(self) -> bool:
        """I4: the whole mission plus recovery plus reserve fits, with room to spare."""
        return self.feasible and self.margin_wh >= 0

    def as_row(self) -> tuple[str, int, int, int, str]:
        return (
            self.aircraft_id,
            self.margin_wh,
            self.consumption_wh,
            self.reserve_required_wh,
            self.breaking_term,
        )


@dataclass(frozen=True)
class EnergyScreenResult:
    """Feasible and infeasible screens, with the guarantee the I5 filter holds."""

    candidate_ids: frozenset[str]
    feasible: tuple[EnergyScreen, ...]
    infeasible: tuple[EnergyScreen, ...]

    @property
    def rows(self) -> tuple[EnergyScreen, ...]:
        return (*self.feasible, *self.infeasible)

    def by_id(self) -> dict[str, EnergyScreen]:
        return {row.aircraft_id: row for row in self.rows}

    def screen_for(self, aircraft_id: str) -> EnergyScreen | None:
        for row in self.rows:
            if row.aircraft_id == aircraft_id:
                return row
        return None

    def digest(self) -> str:
        return digest_of(
            [("screened", f"{row.aircraft_id}:{row.margin_wh}:{row.feasible}") for row in self.rows]
        )


def screen_fleet(
    legs: MissionLegs,
    weather: WindSnapshot,
    candidates: Sequence[FleetAircraft],
    *,
    clock_us: int,
    deadline_us: int | None = None,
) -> EnergyScreenResult:
    """Ask the energy kernel for the complete-mission margin of each *candidate*.

    Only aircraft already through :func:`droneops_dispatcher.eligibility.candidate_query`
    may be passed here; the result asserts that nothing else slipped in, so a
    maintenance tail cannot re-enter the ranking through the energy layer.
    """
    shape = legs.to_shape(weather, clock_us=clock_us, deadline_us=deadline_us)
    unknown: list[EnergyScreen] = []
    states: list[AircraftEnergyState] = []
    for aircraft in candidates:
        try:
            profile = load_profile(aircraft.profile_id, aircraft.profile_version)
        except EnergyError as error:
            unknown.append(
                EnergyScreen(
                    aircraft_id=aircraft.aircraft_id,
                    status=aircraft.status.value,
                    feasible=False,
                    margin_wh=0,
                    consumption_wh=0,
                    reserve_required_wh=0,
                    breaking_term="profile",
                    reason_codes=("profile_unknown",),
                    profile_id=aircraft.profile_id,
                    profile_version=aircraft.profile_version,
                    parameters_sha256="unversioned",
                    disclaimer=str(error),
                    note="no such profile version: the margin cannot be computed",
                )
            )
            continue
        states.append(
            AircraftEnergyState(
                aircraft_id=aircraft.aircraft_id,
                profile=profile,
                charge_bp=aircraft.charge_bp,
                soh_bp=aircraft.soh_bp,
                status=aircraft.status.value,
            )
        )

    assessment = assess_fleet(shape, states)
    screened = _ordered(
        tuple(_screen_of(row) for row in (*assessment.feasible, *assessment.rejected))
        + tuple(unknown)
    )
    ids = frozenset(row.aircraft_id for row in screened)
    offered = frozenset(row.aircraft_id for row in candidates)
    if ids != offered:
        raise DispatchInputError(
            f"energy screen changed the candidate set: {sorted(ids ^ offered)}"
        )
    return EnergyScreenResult(
        candidate_ids=offered,
        feasible=_ordered(tuple(row for row in screened if row.clears_reserve)),
        infeasible=_ordered(tuple(row for row in screened if not row.clears_reserve)),
    )


def _screen_of(row: CandidateAssessment) -> EnergyScreen:
    """The kernel's answer, re-expressed with the reason codes the record needs."""
    codes = [reason.value for reason in row.reasons]
    if row.note and not codes:
        codes.append("energy_model_refused")
    return EnergyScreen(
        aircraft_id=row.aircraft_id,
        status=row.status,
        feasible=row.feasible,
        margin_wh=0 if row.margin_wh is None else row.margin_wh,
        consumption_wh=row.estimated_consumption_wh,
        reserve_required_wh=row.reserve_required_wh,
        breaking_term="unknown" if row.breaking_term is None else row.breaking_term.value,
        reason_codes=tuple(sorted(codes)),
        profile_id=row.profile_id,
        profile_version=row.report.profile.version if row.report is not None else "unknown",
        parameters_sha256=row.parameters_sha256,
        disclaimer=row.disclaimer,
        note=row.note,
    )


def _ordered(rows: Iterable[EnergyScreen]) -> tuple[EnergyScreen, ...]:
    return tuple(sorted(rows, key=lambda row: (-row.margin_wh, row.aircraft_id)))


@dataclass(frozen=True)
class FacilityWaits:
    """Pad, crew and charger wait, kept apart on the way into the metrics."""

    pad_wait_us: int = 0
    crew_wait_us: int = 0
    charger_wait_us: int = 0

    @property
    def total_us(self) -> int:
        return self.pad_wait_us + self.crew_wait_us + self.charger_wait_us

    @classmethod
    def from_events(cls, events: Iterable[WaitEvent]) -> FacilityWaits:
        pad = 0
        crew = 0
        charger = 0
        for event in events:
            if event.reason is WaitReason.PAD_WAIT:
                pad += event.duration_us
            elif event.reason is WaitReason.CREW_UNAVAILABLE:
                crew += event.duration_us
            elif event.reason is WaitReason.CHARGER_WAIT:
                charger += event.duration_us
        return cls(pad_wait_us=pad, crew_wait_us=crew, charger_wait_us=charger)

    def units(self, policy: DispatchPolicy) -> int:
        return (
            self.pad_wait_us // MICROS_PER_MINUTE * policy.pad_wait_units_per_minute
            + self.crew_wait_us // MICROS_PER_MINUTE * policy.crew_wait_units_per_minute
            + self.charger_wait_us // MICROS_PER_MINUTE * policy.charger_wait_units_per_minute
        )

    def as_rows(self) -> tuple[tuple[str, int], ...]:
        return (
            ("pad_wait_us", self.pad_wait_us),
            ("crew_wait_us", self.crew_wait_us),
            ("charger_wait_us", self.charger_wait_us),
        )


@dataclass(frozen=True)
class RepositioningLeg:
    """Geometry the caller owns: how far this tail sits from the alternate site."""

    target_facility_id: str
    leg_mm: int
    climb_mm: int = 0

    def __post_init__(self) -> None:
        if self.leg_mm < 0:
            raise DispatchInputError("a repositioning leg cannot be negative")


@dataclass(frozen=True)
class RepositioningCost:
    """One alternate site, priced. Facilities described it; this decides its cost."""

    request_id: str
    origin_facility_id: str
    target_facility_id: str
    reason: str
    leg_mm: int
    leg_us: int
    energy_wh: int
    origin_wait_us: int
    wait_avoided_us: int
    blocked_penalty_units: int
    units: int
    comparable: bool
    excluded_reason: str
    priced_by: str
    policy_version: str

    def as_row(self) -> tuple[str, str, int, int, int]:
        return (self.target_facility_id, self.reason, self.energy_wh, self.leg_us, self.units)

    def metrics(self) -> tuple[tuple[str, object], ...]:
        return (
            ("target_facility_id", self.target_facility_id),
            ("reposition_leg_mm", self.leg_mm),
            ("reposition_leg_us", self.leg_us),
            ("reposition_energy_wh", self.energy_wh),
            ("reposition_wait_avoided_us", self.wait_avoided_us),
            ("reposition_units", self.units),
            ("reposition_comparable", self.comparable),
            ("priced_by", self.priced_by),
            ("policy_version", self.policy_version),
        )


def price_repositioning(
    quotes: Sequence[AlternateSiteQuote],
    legs: Sequence[RepositioningLeg],
    *,
    policy: DispatchPolicy,
    cruise_power_w: int,
    reference_airspeed_dmps: int,
) -> tuple[RepositioningCost, ...]:
    """Turn ``priced_by="dispatcher"`` data into a ranking input.

    Energy is the profile's own cruise power over the leg time, and leg time is the
    profile's reference airspeed - the same versioned numbers the mission was
    charged with, so a repositioning leg is never cheaper than flying it would be.
    Waiting is the alternative, so the credit for a shorter wait is netted off; a
    site that cannot start anything at all carries an explicit occupancy penalty
    instead of being quietly preferred.
    """
    if cruise_power_w <= 0 or reference_airspeed_dmps <= 0:
        raise DispatchInputError(
            "repositioning needs a declared cruise power and reference airspeed "
            "from the airframe profile; inventing one is worse than refusing to price"
        )
    by_leg = {leg.target_facility_id: leg for leg in legs}
    costs: list[RepositioningCost] = []
    for quote in quotes:
        leg = by_leg.get(quote.target_facility_id)
        excluded = quote.excluded_reason or ""
        comparable = quote.comparable and leg is not None
        if leg is None:
            excluded = excluded or INCOMPARABLE_NO_GEOMETRY
            leg_mm, leg_us, energy_wh = 0, 0, 0
        else:
            leg_mm = leg.leg_mm
            # Time is distance over the profile's reference airspeed, plus the climb
            # at a declared climb rate; energy is the profile's cruise power over
            # that time, rounded *up* - a repositioning leg is never cheaper than
            # flying it, and integer division must not make a leg free.
            leg_us = leg_mm * 10_000 // (reference_airspeed_dmps * 100)
            leg_us += leg.climb_mm * MICROS_PER_SECOND // CLIMB_RATE_MM_PER_S
            energy_wh = -(-cruise_power_w * leg_us // MICROS_PER_HOUR)
        if not quote.comparable and not excluded:
            excluded = INCOMPARABLE_EXCLUDED
        wait_avoided = max(0, quote.origin_wait_us - leg_us)
        penalty = (
            0
            if quote.target.can_start_something and quote.target.crew_free
            else policy.repositioning_pad_occupancy_units
        )
        units = (
            energy_wh * policy.repositioning_energy_units_per_wh
            + leg_us // MICROS_PER_MINUTE * policy.repositioning_time_units_per_minute
            + penalty
            - wait_avoided // MICROS_PER_MINUTE * policy.pad_wait_units_per_minute
        )
        costs.append(
            RepositioningCost(
                request_id=quote.request_id,
                origin_facility_id=quote.origin_facility_id,
                target_facility_id=quote.target_facility_id,
                reason=quote.reason.value,
                leg_mm=leg_mm,
                leg_us=leg_us,
                energy_wh=energy_wh,
                origin_wait_us=quote.origin_wait_us,
                wait_avoided_us=wait_avoided,
                blocked_penalty_units=penalty,
                units=units,
                comparable=comparable,
                excluded_reason=excluded,
                priced_by=quote.priced_by,
                policy_version=policy.policy_version,
            )
        )
    return tuple(sorted(costs, key=lambda row: (row.units, row.target_facility_id)))


def best_repositioning(costs: Sequence[RepositioningCost]) -> RepositioningCost | None:
    """Cheapest comparable alternate, or ``None`` - which means "wait here"."""
    usable = [row for row in costs if row.comparable]
    if not usable:
        return None
    return min(usable, key=lambda row: (row.units, row.target_facility_id))


@dataclass(frozen=True)
class AssignmentScore:
    """One aircraft-mission pair, with every component still visible."""

    mission_id: str
    aircraft_id: str
    energy_margin_wh: int
    lateness_us: int
    waits: FacilityWaits
    reposition: RepositioningCost | None
    earliest_start_us: int
    duration_us: int
    units: int

    def components(self) -> tuple[tuple[str, int], ...]:
        rows: list[tuple[str, int]] = [
            ("energy_margin_wh", self.energy_margin_wh),
            ("lateness_us", self.lateness_us),
            *[(key, value) for key, value in self.waits.as_rows()],
            ("earliest_start_us", self.earliest_start_us),
            ("duration_us", self.duration_us),
            ("total_units", self.units),
        ]
        if self.reposition is not None:
            rows.append(("reposition_energy_wh", self.reposition.energy_wh))
            rows.append(("reposition_units", self.reposition.units))
        return tuple(rows)

    def as_row(self) -> tuple[str, str, int, int, int]:
        return (
            self.mission_id,
            self.aircraft_id,
            self.units,
            self.energy_margin_wh,
            self.lateness_us,
        )

    def digest(self) -> str:
        return digest_of([(key, value) for key, value in self.components()])


def score_candidate(
    *,
    mission_id: str,
    aircraft_id: str,
    margin_wh: int,
    earliest_start_us: int,
    duration_us: int,
    deadline_us: int,
    waits: FacilityWaits,
    policy: DispatchPolicy,
    reposition: RepositioningCost | None = None,
) -> AssignmentScore:
    """Rank cost: late, stuck on a pad, short of margin and flying empty all cost units."""
    if duration_us < 0:
        raise DispatchInputError("duration cannot be negative")
    lateness_us = max(0, earliest_start_us + duration_us - deadline_us)
    lateness_units = lateness_us // MICROS_PER_MINUTE * policy.lateness_penalty_units_per_minute
    reposition_units = 0 if reposition is None else reposition.units
    margin_credit = margin_wh * policy.energy_margin_credit_units_per_wh
    return AssignmentScore(
        mission_id=mission_id,
        aircraft_id=aircraft_id,
        energy_margin_wh=margin_wh,
        lateness_us=lateness_us,
        waits=waits,
        reposition=reposition,
        earliest_start_us=earliest_start_us,
        duration_us=duration_us,
        units=lateness_units + waits.units(policy) + reposition_units - margin_credit,
    )


def rank_scores(scores: Sequence[AssignmentScore]) -> tuple[AssignmentScore, ...]:
    """Total order: cheapest, then widest margin, then least late, then identifier."""
    return tuple(
        sorted(
            scores,
            key=lambda row: (
                row.units,
                -row.energy_margin_wh,
                row.lateness_us,
                row.aircraft_id,
            ),
        )
    )


__all__ = [
    "INCOMPARABLE_EXCLUDED",
    "INCOMPARABLE_NO_GEOMETRY",
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
]
