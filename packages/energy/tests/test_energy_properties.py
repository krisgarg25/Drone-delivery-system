"""Property tests: the margin's shape, not its value.

The three monotonicities below are what make a signed margin usable for ranking.
If margin rose with distance, or fell with a stronger headwind, dispatch would be
ordering plans on noise. Hypothesis searches the input space rather than a handful
of examples, and every failing case shrinks to a reported counter-example.

What these properties deliberately do **not** claim: that a tailwind always helps.
It does not - a strong crosswind forces a crab angle that costs more ground speed
than the along-track component returns. That is why the wind property is stated for
an opposed wind, and why a wind-direction sweep is its own knob in the sensitivity
report.
"""

from __future__ import annotations

from collections.abc import Sequence

from energy_support import BASE_US, KM_MM, MINUTE_US, handover, route, trace
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from droneops_energy import (
    AIRFRAME_X_SIM,
    BatteryProfileVersion,
    MissionEnergyRequest,
    Route,
    WindSnapshot,
    assess_mission,
    snapshot_from_rows,
)

AIRSPEED_DMPS = 120

#: Every term is rounded up to a whole watt-hour (never down, so a mission is never
#: reported cheaper than it costs). Comparing two missions therefore carries a
#: documented slack of one watt-hour per term, at most, in the opposite direction.
#: This is the model's only tolerance, and it is one watt-hour out of hundreds.
ROUNDING_SLACK_WH = 4

#: Below the profile's 80 dm/s limit and below the airspeed, so a leg always makes
#: forward progress and the monotonicity claims are about cost, not blockage.
wind_speeds = st.integers(min_value=0, max_value=70)
payloads = st.integers(min_value=0, max_value=1800)
distances_km = st.integers(min_value=1, max_value=25)
charges = st.integers(min_value=2000, max_value=10_000)


def opposed_trace(speed_dmps: int, *, course_mdeg: int = 90_000) -> WindSnapshot:
    """A frozen trace blowing straight down the outbound track: a pure headwind."""
    bearing = (course_mdeg + 180_000) % 360_000
    rows = [(minute, speed_dmps, bearing, speed_dmps) for minute in range(120)]
    return snapshot_from_rows(
        rows,
        station="IXC-synthetic",
        source="property test frozen trace",
        bundle_sha256="c" * 64,
        retrieved_at_us=BASE_US,
        expires_at_us=BASE_US + 120 * MINUTE_US,
    )


def build(
    profile: BatteryProfileVersion,
    *,
    legs: Sequence[int],
    payload_g: int,
    weather: WindSnapshot,
    charge_bp: int = 10_000,
    soh_bp: int = 10_000,
) -> MissionEnergyRequest:
    """A round trip: out along the legs, back along the same legs reversed."""
    outbound = Route(
        label="outbound",
        legs=tuple(route("out", km * KM_MM, course_mdeg=90_000).legs[0] for km in legs),
    )
    recovery = Route(
        label="recovery",
        legs=tuple(route("back", km * KM_MM, course_mdeg=270_000).legs[0] for km in reversed(legs)),
    )
    return MissionEnergyRequest(
        mission_id="prop-mission",
        payload_g=payload_g,
        outbound=outbound,
        handover=handover(hover_us=30 * MINUTE_US),
        recovery=recovery,
        weather=weather,
        profile=profile,
        charge_bp=charge_bp,
        soh_bp=soh_bp,
        clock_us=BASE_US,
    )


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(small=distances_km, big=distances_km, payload=payloads, wind=wind_speeds)
def test_margin_never_rises_with_distance(small: int, big: int, payload: int, wind: int) -> None:
    """Longer route, same everything else: the signed margin must not improve."""
    near = small
    far = small + big
    weather = opposed_trace(wind)

    short = assess_mission(build(profile_of(), legs=[near], payload_g=payload, weather=weather))
    long = assess_mission(build(profile_of(), legs=[far], payload_g=payload, weather=weather))

    assert long.margin_wh <= short.margin_wh + ROUNDING_SLACK_WH
    assert long.total_required_wh >= short.total_required_wh - ROUNDING_SLACK_WH
    assert long.outbound.flight_us >= short.outbound.flight_us


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(light=payloads, heavy=payloads, distance=distances_km)
def test_margin_never_rises_with_payload(light: int, heavy: int, distance: int) -> None:
    """A heavier aircraft costs more to lift and to climb, so the margin shrinks."""
    payload_low = min(light, 1000)
    payload_high = min(2000, payload_low + heavy)
    weather = trace()

    up = assess_mission(
        build(profile_of(), legs=[distance], payload_g=payload_low, weather=weather)
    )
    down = assess_mission(
        build(profile_of(), legs=[distance], payload_g=payload_high, weather=weather)
    )

    assert down.margin_wh <= up.margin_wh + ROUNDING_SLACK_WH
    assert down.estimated_consumption_wh >= up.estimated_consumption_wh - ROUNDING_SLACK_WH


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(calm=wind_speeds, gusty_add=st.integers(min_value=0, max_value=30), distance=distances_km)
def test_a_stronger_headwind_never_improves_the_margin(
    calm: int, gusty_add: int, distance: int
) -> None:
    """Wind opposed to the track: more of it can only cost margin, never buy it.

    This is the property a boolean check would hide - the same mission stays
    feasible over a range of wind and then crosses, and the crossing is visible in
    the numbers.
    """
    weaker = min(70, calm)
    stronger = min(70, calm + gusty_add)

    mild = assess_mission(
        build(profile_of(), legs=[distance], payload_g=500, weather=opposed_trace(weaker))
    )
    harsh = assess_mission(
        build(profile_of(), legs=[distance], payload_g=500, weather=opposed_trace(stronger))
    )

    assert mild.feasible or not harsh.feasible, "feasibility cannot return as wind strengthens"
    assert harsh.margin_wh <= mild.margin_wh + ROUNDING_SLACK_WH
    assert harsh.estimated_consumption_wh >= mild.estimated_consumption_wh - ROUNDING_SLACK_WH


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(charge=charges, distance=distances_km)
def test_available_energy_is_monotone_in_charge_and_limited_by_the_pack(
    charge: int, distance: int
) -> None:
    """SoC and degradation only ever scale the number, they never flip its meaning."""
    low = assess_mission(
        build(profile_of(), legs=[distance], payload_g=0, weather=trace(), charge_bp=charge)
    )
    high = assess_mission(
        build(profile_of(), legs=[distance], payload_g=0, weather=trace(), charge_bp=10_000)
    )

    assert high.available_wh >= low.available_wh
    assert high.margin_wh >= low.margin_wh - ROUNDING_SLACK_WH
    assert high.available_wh <= high.profile.capacity_wh


@settings(max_examples=30, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(distance=distances_km, payload=payloads, wind=wind_speeds, charge=charges)
def test_every_report_is_a_signed_integer_total_that_adds_up(
    distance: int, payload: int, wind: int, charge: int
) -> None:
    """Whatever the inputs: terms sum to the total, and the margin is their difference.

    The identity is the reason dispatch can rank plans from different airframes:
    the same arithmetic produced every number.
    """
    report = assess_mission(
        build(
            profile_of(),
            legs=[distance],
            payload_g=payload,
            weather=trace(speed_dmps=wind),
            charge_bp=charge,
        )
    )
    charged = sum(row.wh for row in report.terms)

    assert isinstance(report.margin_wh, int)
    assert report.total_required_wh == charged
    assert report.margin_wh == report.available_wh - report.total_required_wh
    assert report.estimated_consumption_wh <= report.total_required_wh
    assert report.reserve_required_wh >= 0
    assert report.feasible == (report.margin_wh >= 0 and not report.reasons)
    assert report.disclaimer.startswith("Energy basis is ILLUSTRATIVE")


def profile_of() -> BatteryProfileVersion:
    """The committed illustrative profile: properties are about its shape, not its size."""
    return AIRFRAME_X_SIM
