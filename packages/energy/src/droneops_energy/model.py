"""The consumption model: a route flown against a frozen wind trace.

Every quantity is an integer and every term is attributable, because the model
exists to answer "how close to the edge is this plan and which part put it
there", not just "does it fit". Inputs follow the architecture section 10 list:
empty and loaded mass, payload mass, route distance, wind, speed profile, climb
and descent, hover/handover time, battery state and degradation, reserve
requirement and recovery or alternate-site cost.

Model structure (all of it driven by profile coefficients, none of it a literal):

* each leg's ground speed comes from the wind triangle at the commanded airspeed,
  so a headwind buys time and time buys power - the asymmetry is emergent rather
  than tabulated;
* cruise energy is power x time, climb adds potential energy at the profile's
  cost-per-kilogram-per-metre, descent returns a tabulated fraction of it and can
  never make a leg's energy negative;
* hover is charged at the profile's hover power, which is the handover cost.

The only floats in this module are the two trigonometric components per leg,
quantised to basis points by :mod:`droneops_energy.trig` (documented tolerance
there: under 5e-5 of the wind vector, far below the watt-hour output resolution).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from itertools import pairwise

from .errors import EnergyInputError, NoProgressError, RouteBlockedError, TrackHoldingError
from .numeric import BP_PER_UNIT, US_PER_S, ceil_div, require, watt_us_to_mwh
from .profile import BatteryProfileVersion
from .trig import normalize_mdeg, wind_components_bp
from .weather import US_PER_MINUTE, WindSample, WindSnapshot

#: Millimetres per second in one deci-metre per second.
MM_PER_S_PER_DMPS = 100


@dataclass(frozen=True)
class Leg:
    """One ground-track segment: distance, vertical profile, course and hover."""

    distance_mm: int
    course_mdeg: int
    climb_mm: int = 0
    descent_mm: int = 0
    hover_us: int = 0
    airspeed_dmps: int | None = None

    def __post_init__(self) -> None:
        require(self.distance_mm >= 0, "a leg cannot be shorter than zero")
        require(self.climb_mm >= 0 and self.descent_mm >= 0, "climb and descent are magnitudes")
        require(self.hover_us >= 0, "hover time cannot be negative")
        if self.airspeed_dmps is not None:
            require(self.airspeed_dmps > 0, "commanded airspeed must be positive")

    @property
    def is_static(self) -> bool:
        """A zero-length, zero-climb leg only ever costs hover energy."""
        return self.distance_mm == 0 and self.climb_mm == 0 and self.descent_mm == 0

    def at_airspeed(self, airspeed_dmps: int) -> Leg:
        """Pin an explicit airspeed, overriding the speed profile."""
        return replace(self, airspeed_dmps=airspeed_dmps)


@dataclass(frozen=True)
class Route:
    """An ordered path. Order matters: wind is sampled as the clock advances."""

    label: str
    legs: tuple[Leg, ...] = ()

    @classmethod
    def straight(
        cls,
        label: str,
        distance_mm: int,
        *,
        course_mdeg: int = 0,
        climb_mm: int = 0,
        descent_mm: int = 0,
    ) -> Route:
        """A single-leg route, the shape a corridor edge reduces to."""
        return cls(label=label, legs=(Leg(distance_mm, course_mdeg, climb_mm, descent_mm),))

    @classmethod
    def idle(cls, label: str, *, hover_us: int) -> Route:
        """Station-keeping only: the handover or loiter case."""
        return cls(label=label, legs=(Leg(0, 0, hover_us=hover_us),))

    def __post_init__(self) -> None:
        require(self.label != "", "a route needs a label so a rejection can name it")

    @property
    def distance_mm(self) -> int:
        return sum(leg.distance_mm for leg in self.legs)

    @property
    def climb_mm(self) -> int:
        return sum(leg.climb_mm for leg in self.legs)

    @property
    def descent_mm(self) -> int:
        return sum(leg.descent_mm for leg in self.legs)

    @property
    def hover_us(self) -> int:
        return sum(leg.hover_us for leg in self.legs)

    @property
    def is_empty(self) -> bool:
        """No distance and no hovering: a zero-cost recovery, e.g. already home."""
        return self.distance_mm == 0 and self.climb_mm == 0 and self.hover_us == 0

    def scaled_by_bp(self, factor_bp: int) -> Route:
        """Scale horizontal distances only: the sensitivity knob for route length."""
        return replace(
            self,
            legs=tuple(
                replace(leg, distance_mm=ceil_div(leg.distance_mm * factor_bp, BP_PER_UNIT))
                for leg in self.legs
            ),
        )

    def with_hover_us(self, hover_us: int) -> Route:
        """Replace the route's total station-keeping time, held on the first leg.

        Charging it per leg would multiply the wait by the number of legs, which is
        not what "hover for sixty seconds" means.
        """
        legs = self.legs or (Leg(0, 0),)
        return replace(self, legs=(replace(legs[0], hover_us=hover_us), *legs[1:]))


@dataclass(frozen=True)
class SpeedStep:
    """Commanded airspeed from a leg index onward."""

    first_leg: int
    airspeed_dmps: int


@dataclass(frozen=True)
class SpeedProfile:
    """A named airspeed policy: the speed profile the spec lists as an input.

    Steps are resolved by leg index against the *outbound* route; the recovery leg
    uses the last step, since it flies the same kind of corridor empty.
    """

    steps: tuple[SpeedStep, ...] = ()
    default_airspeed_dmps: int | None = None

    #: Resolved against the outbound route's leg index.

    def __post_init__(self) -> None:
        ordered = sorted(self.steps, key=lambda step: step.first_leg)
        for left, right in pairwise(ordered):
            require(left.first_leg != right.first_leg, "speed steps must name distinct leg indices")
        object.__setattr__(self, "steps", tuple(ordered))
        if self.default_airspeed_dmps is not None:
            require(self.default_airspeed_dmps > 0, "default airspeed must be positive")

    def resolve(self, leg_index: int, profile: BatteryProfileVersion) -> int:
        """Airspeed for a leg: explicit step, else the profile's cruise speed."""
        chosen = profile.cruise_airspeed_dmps
        for step in self.steps:
            if step.first_leg <= leg_index:
                chosen = step.airspeed_dmps
        if self.default_airspeed_dmps is not None and not self.steps:
            chosen = self.default_airspeed_dmps
        return chosen

    def fastest_step(self) -> int | None:
        """Highest commanded airspeed in the profile, for the profile-capability check."""
        if not self.steps:
            return self.default_airspeed_dmps
        return max(step.airspeed_dmps for step in self.steps)


@dataclass(frozen=True)
class LegEnergy:
    """Full attribution for one leg: the numbers a reviewer asks for."""

    index: int
    minute: int
    distance_mm: int
    airspeed_dmps: int
    ground_speed_mm_per_s: int
    along_wind_dmps: int
    cross_wind_dmps: int
    flight_us: int
    hover_us: int
    power_w: int
    cruise_mwh: int
    climb_mwh: int
    descent_credit_mwh: int
    hover_mwh: int
    total_mwh: int

    @property
    def wind_is_head(self) -> bool:
        """Negative along-track component: the aircraft is fighting the trace."""
        return self.along_wind_dmps < 0


@dataclass(frozen=True)
class RouteEnergy:
    """Cost and elapsed time for one route, and the clock it assumed."""

    label: str
    start_us: int
    end_us: int
    legs: tuple[LegEnergy, ...] = ()
    blocked_reason: str | None = None

    @property
    def energy_mwh(self) -> int:
        return sum(row.total_mwh for row in self.legs)

    @property
    def flight_us(self) -> int:
        return sum(row.flight_us for row in self.legs)

    @property
    def hover_us(self) -> int:
        return sum(row.hover_us for row in self.legs)

    @property
    def worst_headwind_dmps(self) -> int:
        return min((row.along_wind_dmps for row in self.legs), default=0)


def _ground_speed(
    *, airspeed_dmps: int, sustained: WindSample, course_mdeg: int, leg_index: int
) -> tuple[int, int, int]:
    """Solve the wind triangle: ``(ground_mm_per_s, along_dmps, cross_dmps)``.

    Rounding is always adverse: the along component floors (less tailwind, more
    headwind) and the cross magnitude ceils (more crab, less forward speed).
    """
    along_bp, cross_bp = wind_components_bp(sustained.bearing_mdeg, normalize_mdeg(course_mdeg))
    along_dmps = sustained.speed_dmps * along_bp // BP_PER_UNIT
    cross_dmps = ceil_div(sustained.speed_dmps * abs(cross_bp), BP_PER_UNIT)
    if cross_dmps >= airspeed_dmps:
        raise TrackHoldingError(
            f"leg {leg_index}: crosswind {cross_dmps} dm/s meets or exceeds airspeed "
            f"{airspeed_dmps} dm/s, so no heading holds this track",
            leg_index=leg_index,
        )
    air_mm_per_s = airspeed_dmps * MM_PER_S_PER_DMPS
    cross_mm_per_s = cross_dmps * MM_PER_S_PER_DMPS
    forward_mm_per_s = math.isqrt(air_mm_per_s * air_mm_per_s - cross_mm_per_s * cross_mm_per_s)
    ground_mm_per_s = forward_mm_per_s + along_dmps * MM_PER_S_PER_DMPS
    if ground_mm_per_s <= 0:
        raise NoProgressError(
            f"leg {leg_index}: headwind {abs(along_dmps)} dm/s cancels the {airspeed_dmps} dm/s "
            "airspeed, so the leg never completes",
            leg_index=leg_index,
        )
    return ground_mm_per_s, along_dmps, cross_dmps


def fly_leg(
    leg: Leg,
    *,
    index: int,
    at_us: int,
    weather: WindSnapshot,
    mass_g: int,
    profile_version: BatteryProfileVersion,
    soh_bp: int,
    airspeed_dmps: int,
) -> LegEnergy:
    """Cost one leg at the wind in force when it starts, advancing the clock."""
    require(mass_g > 0, "all-up mass must be positive")
    sustained = weather.sample_at_us(at_us)
    ground_mm_per_s, along_dmps, cross_dmps = _ground_speed(
        airspeed_dmps=airspeed_dmps,
        sustained=sustained,
        course_mdeg=leg.course_mdeg,
        leg_index=index,
    )
    flight_us = 0 if leg.distance_mm == 0 else ceil_div(leg.distance_mm * US_PER_S, ground_mm_per_s)
    power_w = profile_version.power_w(mass_g=mass_g, airspeed_dmps=airspeed_dmps, soh_bp=soh_bp)
    cruise_mwh = watt_us_to_mwh(power_w, flight_us)
    climb_mwh = profile_version.climb_mwh(mass_g=mass_g, climb_mm=leg.climb_mm)
    credit_mwh = profile_version.descent_credit_mwh(mass_g=mass_g, descent_mm=leg.descent_mm)
    # A descent cannot pay for the power that keeps the aircraft airborne, so the
    # air-leg total is floored at zero rather than allowed to go negative.
    air_mwh = max(0, cruise_mwh + climb_mwh - credit_mwh)
    hover_mwh = watt_us_to_mwh(
        profile_version.hover_power_at(mass_g=mass_g, soh_bp=soh_bp), leg.hover_us
    )
    return LegEnergy(
        index=index,
        minute=at_us // US_PER_MINUTE,
        distance_mm=leg.distance_mm,
        airspeed_dmps=airspeed_dmps,
        ground_speed_mm_per_s=ground_mm_per_s,
        along_wind_dmps=along_dmps,
        cross_wind_dmps=cross_dmps,
        flight_us=flight_us,
        hover_us=leg.hover_us,
        power_w=power_w,
        cruise_mwh=cruise_mwh,
        climb_mwh=climb_mwh,
        descent_credit_mwh=credit_mwh,
        hover_mwh=hover_mwh,
        total_mwh=air_mwh + hover_mwh,
    )


def fly_route(
    route: Route,
    *,
    start_us: int,
    weather: WindSnapshot,
    mass_g: int,
    profile_version: BatteryProfileVersion,
    soh_bp: int,
    speed: SpeedProfile,
) -> RouteEnergy:
    """Fly a route leg by leg on a monotonic integer clock.

    The wind for each leg is sampled at the moment the leg starts, which is
    computed from the previous legs: no circular dependency, no re-ordering, so
    two runs over the same trace get the same numbers.
    """
    rows: list[LegEnergy] = []
    clock = start_us
    for index, leg in enumerate(route.legs):
        airspeed = leg.airspeed_dmps or speed.resolve(index, profile_version)
        if airspeed > profile_version.cruise_airspeed_dmps * 2:
            raise EnergyInputError(
                f"{profile_version.profile_id}: commanded airspeed {airspeed} dm/s is unreviewed "
                "for this profile"
            )
        try:
            row = fly_leg(
                leg,
                index=index,
                at_us=clock,
                weather=weather,
                mass_g=mass_g,
                profile_version=profile_version,
                soh_bp=soh_bp,
                airspeed_dmps=airspeed,
            )
        except RouteBlockedError as blocked:
            # The leg is unflyable rather than free: the route stops here and the
            # caller rejects the mission, because its cost is unknown, not zero.
            return RouteEnergy(
                label=route.label,
                start_us=start_us,
                end_us=clock,
                legs=tuple(rows),
                blocked_reason=str(blocked),
            )
        rows.append(row)
        clock += row.flight_us + row.hover_us
    return RouteEnergy(label=route.label, start_us=start_us, end_us=clock, legs=tuple(rows))
