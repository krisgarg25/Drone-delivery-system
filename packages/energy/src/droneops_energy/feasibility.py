"""Complete-mission feasibility (invariant I4) and the signed margin.

I4: *route outbound plus handover plus reserve plus recovery or alternate is
within available energy*. All four terms are charged against one battery, in that
order. A mission whose outbound leg fits but whose recovery does not is rejected,
and the report names the term that broke it.

The result is a **margin**, not a boolean. :attr:`MarginReport.margin_wh` is
signed watt-hours: energy left after the whole mission and its reserve, or the
energy that is missing. Dispatch ranks plans on it and the console shows how close
to the edge a decision was - a pass/fail bit would discard exactly the
information the spec asks to preserve (architecture section 10).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from droneops_contracts.energy import EnergyEstimate

from .errors import EnergyError, EnergyInputError
from .model import Route, RouteEnergy, SpeedProfile, fly_route
from .numeric import BP_PER_UNIT, mwh_to_wh, require
from .profile import BatteryProfileVersion
from .weather import WindSnapshot


class TermName(StrEnum):
    """Named energy term of the complete-mission sum (I4)."""

    OUTBOUND = "outbound"
    HANDOVER = "handover"
    RECOVERY = "recovery"
    RESERVE = "reserve"
    GUST_UPLIFT = "gust_uplift"


#: The order energy is charged in, and therefore the order a rejection is
#: attributed in. ``assess_mission`` walks this tuple; it is not decoration.
TERMS_IN_ORDER: tuple[TermName, ...] = (
    TermName.OUTBOUND,
    TermName.HANDOVER,
    TermName.RECOVERY,
    TermName.RESERVE,
    TermName.GUST_UPLIFT,
)


class RejectReason(StrEnum):
    """Why a mission is not releasable. Rejection paths are first-class results."""

    OUTBOUND_INFEASIBLE = "outbound_infeasible"
    HANDOVER_INFEASIBLE = "handover_infeasible"
    RECOVERY_INFEASIBLE = "recovery_infeasible"
    RESERVE_INFEASIBLE = "reserve_infeasible"
    GUST_UPLIFT_INFEASIBLE = "gust_uplift_infeasible"
    RECOVERY_ROUTE_MISSING = "recovery_route_missing"
    ROUTE_BLOCKED_BY_WIND = "route_blocked_by_wind"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    STALE_WEATHER = "stale_weather"
    SUSTAINED_WIND_ABOVE_AIRFRAME_LIMIT = "sustained_wind_above_airframe_limit"
    BATTERY_MODELS_DISAGREE = "battery_models_disagree"


_REASON_TERM = {
    RejectReason.OUTBOUND_INFEASIBLE: TermName.OUTBOUND,
    RejectReason.HANDOVER_INFEASIBLE: TermName.HANDOVER,
    RejectReason.RECOVERY_INFEASIBLE: TermName.RECOVERY,
    RejectReason.RECOVERY_ROUTE_MISSING: TermName.RECOVERY,
    RejectReason.RESERVE_INFEASIBLE: TermName.RESERVE,
    RejectReason.GUST_UPLIFT_INFEASIBLE: TermName.GUST_UPLIFT,
}


@dataclass(frozen=True)
class TermAttribution:
    """One term's cost, the running total, and the margin that survives it."""

    term: TermName
    wh: int
    cumulative_wh: int
    margin_after_wh: int


@dataclass(frozen=True)
class MissionEnergyRequest:
    """Every input the architecture section 10 list names, in one immutable object."""

    mission_id: str
    payload_g: int
    outbound: Route
    handover: Route
    recovery: Route | None
    weather: WindSnapshot
    profile: BatteryProfileVersion
    charge_bp: int = BP_PER_UNIT
    soh_bp: int = BP_PER_UNIT
    speed: SpeedProfile = field(default_factory=SpeedProfile)
    clock_us: int = 0
    deadline_us: int | None = None
    requires_alternate: bool = False

    def __post_init__(self) -> None:
        require(self.mission_id != "", "a feasibility question needs a mission id")
        require(self.payload_g >= 0, "payload mass cannot be negative")
        require(self.clock_us >= 0, "the scenario clock is integer microseconds from the epoch")
        require(0 <= self.charge_bp <= BP_PER_UNIT, "battery charge must be in basis points")
        require(0 <= self.soh_bp <= BP_PER_UNIT, "battery state of health must be basis points")
        if self.deadline_us is not None:
            require(self.deadline_us >= self.clock_us, "a deadline cannot precede the clock")


@dataclass(frozen=True)
class MarginReport:
    """Signed-margin feasibility report for one aircraft and one complete mission."""

    mission_id: str
    profile: BatteryProfileVersion
    weather: WindSnapshot
    available_wh: int
    estimated_consumption_wh: int
    reserve_required_wh: int
    total_required_wh: int
    margin_wh: int
    terms: tuple[TermAttribution, ...]
    feasible: bool
    reasons: tuple[RejectReason, ...]
    breaking_term: TermName | None
    outbound: RouteEnergy
    handover: RouteEnergy
    recovery: RouteEnergy | None
    arrival_us: int
    recovery_end_us: int
    peak_gust_dmps: int
    sustained_wind_dmps: int

    # ------------------------------------------------------------- I4 evidence
    @property
    def summary(self) -> str:
        """One line a console can show, with the margin and what broke it."""
        verdict = "FEASIBLE" if self.feasible else "REJECTED"
        where = f", breaking term: {self.breaking_term.value}" if self.breaking_term else ""
        return (
            f"{verdict} margin={self.margin_wh:+d} Wh "
            f"(available {self.available_wh} Wh, required {self.total_required_wh} Wh{where})"
        )

    @property
    def evidence(self) -> tuple[tuple[str, int], ...]:
        """The I4 sum term by term, for a decision record."""
        rows: list[tuple[str, int]] = [(row.term.value, row.wh) for row in self.terms]
        rows.append(("estimated_consumption", self.estimated_consumption_wh))
        rows.append(("reserve_required", self.reserve_required_wh))
        rows.append(("total_required", self.total_required_wh))
        rows.append(("available", self.available_wh))
        rows.append(("margin", self.margin_wh))
        return tuple(rows)

    @property
    def disclaimer(self) -> str:
        """The basis statement, always attached; no caller can strip it by choice."""
        return self.profile.basis

    @property
    def profile_sha256(self) -> str:
        """The pinned energy basis of this decision."""
        return self.profile.parameters_sha256

    @property
    def routes_complete(self) -> bool:
        """True only when every leg of every route was actually flyable."""
        rows = (self.outbound, self.handover) + ((self.recovery,) if self.recovery else ())
        return all(row.blocked_reason is None for row in rows)

    def term(self, name: TermName) -> TermAttribution:
        """Look one term up by name."""
        for row in self.terms:
            if row.term is name:
                return row
        raise EnergyInputError(f"report does not carry a term named {name}")

    def margin_bp_of_available(self) -> int:
        """Margin as basis points of available energy: the scale-free ranking key."""
        if self.available_wh <= 0:
            return 0
        return self.margin_wh * BP_PER_UNIT // self.available_wh

    def rank_key(self) -> tuple[int, str]:
        """Deterministic ranking key for dispatch: signed margin, then profile id."""
        return (-self.margin_wh, self.profile.profile_id)

    def to_energy_estimate(self) -> EnergyEstimate:
        """The contract record for an accepted plan, or a refusal.

        ``EnergyEstimate.margin_wh`` is non-negative by schema, so this is only
        constructible for a feasible complete mission: an infeasible plan must not
        reach a committed record with its deficit masked as zero.
        """
        if not self.feasible:
            raise EnergyError(
                f"{self.mission_id}: cannot emit an EnergyEstimate for an infeasible mission "
                f"({self.summary})"
            )
        return EnergyEstimate(
            profile_id=f"{self.profile.profile_id}#{self.profile_sha256[:12]}",
            estimated_consumption_wh=self.estimated_consumption_wh,
            reserve_required_wh=self.reserve_required_wh,
            margin_wh=self.margin_wh,
        )


@dataclass(frozen=True)
class BlockedLeg:
    """A leg the wind made unflyable, and the mission term it belongs to."""

    term: TermName
    detail: str


def _first_blocked(flown: Sequence[tuple[TermName, RouteEnergy]]) -> BlockedLeg | None:
    """Attribute a wind-blocked leg to the mission term it blocks."""
    for term, route in flown:
        if route.blocked_reason is not None:
            return BlockedLeg(term=term, detail=route.blocked_reason)
    return None


def assess_mission(request: MissionEnergyRequest) -> MarginReport:
    """Score the complete mission: outbound + handover + recovery + reserve."""
    profile = request.profile
    weather = request.weather
    loaded_mass_g = profile.mass_for_payload(request.payload_g)

    outbound = fly_route(
        request.outbound,
        start_us=request.clock_us,
        weather=weather,
        mass_g=loaded_mass_g,
        profile_version=profile,
        soh_bp=request.soh_bp,
        speed=request.speed,
    )
    handover = fly_route(
        request.handover,
        start_us=outbound.end_us,
        weather=weather,
        mass_g=loaded_mass_g,
        profile_version=profile,
        soh_bp=request.soh_bp,
        speed=request.speed,
    )
    recovery: RouteEnergy | None = None
    if request.recovery is not None:
        # The payload is delivered at handover, so recovery flies at empty mass.
        recovery = fly_route(
            request.recovery,
            start_us=handover.end_us,
            weather=weather,
            mass_g=profile.empty_mass_g,
            profile_version=profile,
            soh_bp=request.soh_bp,
            speed=request.speed,
        )

    # Gust uplift is charged on the whole flight, including the empty return leg.
    flown_recovery_mwh = 0 if recovery is None else recovery.energy_mwh
    available_wh = profile.available_wh(soh_bp=request.soh_bp, charge_bp=request.charge_bp)
    reserve_wh = profile.reserve_wh(soh_bp=request.soh_bp, for_alternate=request.requires_alternate)
    recovery_end_us = recovery.end_us if recovery is not None else handover.end_us
    peak_gust_dmps = weather.peak_gust_dmps(request.clock_us, recovery_end_us)
    peak_sustained_dmps = weather.peak_sustained_dmps(request.clock_us, recovery_end_us)
    gust_uplift_bp = profile.gust_uplift_bp(
        sustained_dmps=peak_sustained_dmps, gust_dmps=peak_gust_dmps
    )
    flight_mwh = outbound.energy_mwh + handover.energy_mwh + flown_recovery_mwh
    gust_uplift_wh = mwh_to_wh(flight_mwh * gust_uplift_bp // BP_PER_UNIT)

    flown_recovery_wh = 0 if recovery is None else mwh_to_wh(recovery.energy_mwh)
    charges = {
        TermName.OUTBOUND: mwh_to_wh(outbound.energy_mwh),
        TermName.HANDOVER: mwh_to_wh(handover.energy_mwh),
        TermName.RECOVERY: flown_recovery_wh,
        TermName.RESERVE: reserve_wh,
        TermName.GUST_UPLIFT: gust_uplift_wh,
    }
    terms: list[TermAttribution] = []
    cumulative = 0
    for name in TERMS_IN_ORDER:
        wh = charges[name]
        cumulative += wh
        terms.append(
            TermAttribution(
                term=name,
                wh=wh,
                cumulative_wh=cumulative,
                margin_after_wh=available_wh - cumulative,
            )
        )

    total_required_wh = cumulative
    margin_wh = available_wh - total_required_wh
    consumption = sum(row.wh for row in terms if row.term in _CONSUMED)
    flown: tuple[tuple[TermName, RouteEnergy], ...] = (
        (TermName.OUTBOUND, outbound),
        (TermName.HANDOVER, handover),
    )
    if recovery is not None:
        flown = (*flown, (TermName.RECOVERY, recovery))
    blocked = _first_blocked(flown)
    reasons = _reasons(request, terms, margin_wh, blocked, arrival_us=handover.end_us)
    breaking = _breaking_term(terms, margin_wh, reasons, blocked)

    return MarginReport(
        mission_id=request.mission_id,
        profile=profile,
        weather=weather,
        available_wh=available_wh,
        estimated_consumption_wh=consumption,
        reserve_required_wh=reserve_wh + gust_uplift_wh,
        total_required_wh=total_required_wh,
        margin_wh=margin_wh,
        terms=tuple(terms),
        feasible=margin_wh >= 0 and not reasons,
        reasons=reasons,
        breaking_term=breaking,
        outbound=outbound,
        handover=handover,
        recovery=recovery,
        arrival_us=handover.end_us,
        recovery_end_us=recovery_end_us,
        peak_gust_dmps=peak_gust_dmps,
        sustained_wind_dmps=peak_sustained_dmps,
    )


_INFEASIBLE: dict[TermName, RejectReason] = {
    TermName.OUTBOUND: RejectReason.OUTBOUND_INFEASIBLE,
    TermName.HANDOVER: RejectReason.HANDOVER_INFEASIBLE,
    TermName.RECOVERY: RejectReason.RECOVERY_INFEASIBLE,
    TermName.RESERVE: RejectReason.RESERVE_INFEASIBLE,
    TermName.GUST_UPLIFT: RejectReason.GUST_UPLIFT_INFEASIBLE,
}

#: Terms that are energy actually spent, as opposed to energy that must be held back.
_CONSUMED: frozenset[TermName] = frozenset(
    {TermName.OUTBOUND, TermName.HANDOVER, TermName.RECOVERY}
)


def _reasons(
    request: MissionEnergyRequest,
    terms: Sequence[TermAttribution],
    margin_wh: int,
    blocked: BlockedLeg | None,
    *,
    arrival_us: int,
) -> tuple[RejectReason, ...]:
    """Every reason the mission is not releasable, in stable sorted order."""
    reasons: set[RejectReason] = set()
    weather = request.weather
    if not weather.is_admissible(request.clock_us):
        reasons.add(RejectReason.STALE_WEATHER)
    if weather.sample_at_us(request.clock_us).speed_dmps > request.profile.max_wind_dmps:
        # A demonstrated airframe limit, read from the profile, not a preference.
        reasons.add(RejectReason.SUSTAINED_WIND_ABOVE_AIRFRAME_LIMIT)
    if blocked is not None:
        reasons.add(RejectReason.ROUTE_BLOCKED_BY_WIND)
    if request.recovery is None:
        # Without a recovery plan I4 cannot be proven: uncertainty is never feasible.
        reasons.add(RejectReason.RECOVERY_ROUTE_MISSING)
    for row in terms:
        if row.margin_after_wh < 0:
            reasons.add(_INFEASIBLE[row.term])
    if request.deadline_us is not None and arrival_us > request.deadline_us:
        reasons.add(RejectReason.DEADLINE_EXCEEDED)
    if margin_wh < 0 and not (reasons & set(_REASON_TERM)):
        reasons.add(RejectReason.RESERVE_INFEASIBLE)
    return tuple(sorted(reasons, key=lambda reason: reason.value))


def _breaking_term(
    terms: Sequence[TermAttribution],
    margin_wh: int,
    reasons: Sequence[RejectReason],
    blocked: BlockedLeg | None,
) -> TermName | None:
    """Which term the rejection is attributed to: the first charge to cross zero."""
    if feasible_report(margin_wh, reasons):
        return None
    for row in terms:
        if row.margin_after_wh < 0:
            return row.term
    if blocked is not None:
        return blocked.term
    for reason in reasons:
        term = _REASON_TERM.get(reason)
        if term is not None:
            return term
    return TermName.RESERVE


def feasible_report(margin_wh: int, reasons: Sequence[RejectReason]) -> bool:
    """The one definition of feasibility: no deficit, and no blocking reason."""
    return margin_wh >= 0 and not reasons


def rank_reports(reports: Sequence[MarginReport]) -> tuple[MarginReport, ...]:
    """Rank plans by best margin first; ties broken deterministically."""
    return tuple(sorted(reports, key=lambda report: report.rank_key()))


__all__ = [
    "BlockedLeg",
    "MarginReport",
    "MissionEnergyRequest",
    "RejectReason",
    "TermAttribution",
    "TermName",
    "assess_mission",
    "feasible_report",
    "rank_reports",
]
