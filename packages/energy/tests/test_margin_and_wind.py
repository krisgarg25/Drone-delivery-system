"""The margin, the wind wiring and the versioned basis.

Coverage for the parts of the brief that are not I4: a zero baseline burn, an
impossible deadline, headwind/tailwind asymmetry, wind reversal, the different
routes gusts and sustained wind take into the answer, battery degradation crossing
the threshold, stale snapshots, plan ranking, the contract bridge, and the rule
that a battery-model disagreement is reviewed rather than accepted.

Everything asserted here is a *relation* between two runs of the same kernel, not
an absolute watt-hour figure: the absolute figures are illustrative profile output
and are re-checked only for being whole, signed integers.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from energy_support import (
    BASE_US,
    DUE_EAST_MDEG,
    DUE_WEST_MDEG,
    KM_MM,
    MINUTE_US,
    alt_profile,
    mission,
    trace,
)

from droneops_energy import (
    AIRFRAME_X_SIM,
    AIRFRAME_Y_SIM,
    ILLUSTRATIVE_DISCLAIMER,
    Agreement,
    BatteryProfileVersion,
    Freshness,
    MarginReport,
    RejectReason,
    TermName,
    UnknownProfileError,
    assess_mission,
    compare_battery_models,
    load_profile,
    publish_profile,
    rank_reports,
    snapshot_from_rows,
)
from droneops_energy.crosscheck import ReviewPolicy

WEST_HEADWIND = DUE_WEST_MDEG


def margin(report: MarginReport) -> int:
    return report.margin_wh


# ------------------------------------------------------------------------ zero
def test_zero_energy_idle_mission_costs_exactly_nothing() -> None:
    """No distance, no climb, no hover, no recovery leg: the model must not invent a burn.

    A phantom baseline draw would quietly poison every ranking dispatch does.
    """
    idle = mission(
        distance_mm=0,
        recovery_mm=0,
        hover_us=0,
        climb_mm=0,
        payload_g=0,
        profile=alt_profile(reserve_fraction_bp="0", alternate_fraction_bp="0"),
    )
    report = assess_mission(idle)

    assert report.estimated_consumption_wh == 0
    assert report.outbound.energy_mwh == 0
    assert report.outbound.flight_us == 0
    assert report.total_required_wh == 0
    assert report.margin_wh == report.available_wh
    assert report.feasible


def test_a_still_airframe_with_zero_charge_has_no_available_energy() -> None:
    """Available energy scales to zero with charge: a flat pack is not a free plan."""
    empty = assess_mission(mission(charge_bp=0))

    assert empty.available_wh == 0
    assert empty.margin_wh <= 0
    assert not empty.feasible


# -------------------------------------------------------------------- deadline
def test_impossible_deadline_rejects_even_with_plenty_of_energy() -> None:
    """A mission that fits the battery but not the clock is still not feasible."""
    roomy_battery = mission(distance_mm=3 * KM_MM, recovery_mm=3 * KM_MM, charge_bp=10_000)
    too_late = assess_mission(
        mission(
            distance_mm=3 * KM_MM,
            recovery_mm=3 * KM_MM,
            deadline_us=BASE_US + MINUTE_US,  # one minute: the leg alone takes longer
        )
    )

    assert too_late.margin_wh > 0, "energy is not the problem"
    assert not too_late.feasible
    assert RejectReason.DEADLINE_EXCEEDED in too_late.reasons
    assert too_late.arrival_us > BASE_US + MINUTE_US
    assert assess_mission(roomy_battery).feasible


def test_a_generous_deadline_adds_no_reason() -> None:
    """A 90-minute clock on a 3 km mission must not manufacture a rejection."""
    report = assess_mission(
        mission(
            distance_mm=3 * KM_MM,
            recovery_mm=3 * KM_MM,
            deadline_us=BASE_US + 90 * MINUTE_US,
        )
    )

    assert report.feasible, report.summary
    assert RejectReason.DEADLINE_EXCEEDED not in report.reasons


def test_a_headwind_can_make_the_same_route_miss_its_deadline() -> None:
    """Time and energy share one cause: the wind triangle, not two separate fudges.

    30 km at 12 m/s airspeed is 2500 s. A 6 m/s headwind stretches it past 41
    minutes, so a 40-minute deadline that the calm plan meets is missed with no
    change to the route, the aircraft or the energy available.
    """
    deadline = BASE_US + 40 * MINUTE_US
    calm = assess_mission(
        mission(
            distance_mm=24 * KM_MM,
            recovery_mm=3 * KM_MM,
            weather=trace(speed_dmps=0),
            deadline_us=deadline,
        )
    )
    headwind = assess_mission(
        mission(
            distance_mm=24 * KM_MM,
            recovery_mm=3 * KM_MM,
            weather=trace(speed_dmps=60, bearing_mdeg=WEST_HEADWIND),
            deadline_us=deadline,
        )
    )

    assert calm.feasible, "the same mission meets the same clock in calm air"
    assert not headwind.feasible
    assert RejectReason.DEADLINE_EXCEEDED in headwind.reasons
    assert headwind.arrival_us > calm.arrival_us


# ------------------------------------------------------------------------ wind
def test_headwind_costs_more_than_tailwind_on_the_same_route() -> None:
    """The asymmetry is emergent: a headwind buys time, and time buys power."""
    tailwind = assess_mission(
        mission(
            distance_mm=20 * KM_MM,
            recovery_mm=3 * KM_MM,
            weather=trace(speed_dmps=50, bearing_mdeg=DUE_EAST_MDEG),
        )
    )
    headwind = assess_mission(
        mission(
            distance_mm=20 * KM_MM,
            recovery_mm=3 * KM_MM,
            weather=trace(speed_dmps=50, bearing_mdeg=WEST_HEADWIND),
        )
    )

    assert headwind.estimated_consumption_wh > tailwind.estimated_consumption_wh
    assert headwind.margin_wh < tailwind.margin_wh
    assert headwind.outbound.flight_us > tailwind.outbound.flight_us
    assert headwind.outbound.legs[0].along_wind_dmps < 0
    assert tailwind.outbound.legs[0].along_wind_dmps > 0


def test_reversing_the_wind_on_one_route_changes_the_margin() -> None:
    """Same mission, same aircraft, opposite weather: a different signed margin."""
    east = assess_mission(mission(weather=trace(speed_dmps=60, bearing_mdeg=DUE_EAST_MDEG)))
    reversed_trace = east.weather.reversed()
    west = assess_mission(mission(weather=reversed_trace))

    assert margin(east) != margin(west)
    assert reversed_trace.samples[0].bearing_mdeg == DUE_WEST_MDEG
    # A round trip pays for the wind twice over in one direction and cancels in the
    # other, which is why reversal is not the same test as head/tail asymmetry.
    assert west.outbound.legs[0].ground_speed_mm_per_s < east.outbound.legs[0].ground_speed_mm_per_s


def test_gusts_raise_the_reserve_and_never_the_consumption() -> None:
    """Gusts and sustained wind are wired differently, on purpose."""
    calm_gust = assess_mission(mission(weather=trace(speed_dmps=40, gust_dmps=40)))
    gusty = assess_mission(mission(weather=trace(speed_dmps=40, gust_dmps=120)))

    assert gusty.estimated_consumption_wh == calm_gust.estimated_consumption_wh
    assert gusty.reserve_required_wh > calm_gust.reserve_required_wh
    assert gusty.terms[-1].term is TermName.GUST_UPLIFT
    assert gusty.terms[-1].wh > calm_gust.terms[-1].wh
    assert gusty.margin_wh < calm_gust.margin_wh
    assert gusty.peak_gust_dmps == 120


def test_sustained_wind_changes_consumption_and_does_not_need_a_gust() -> None:
    """The other half of the wiring: sustained wind is kinematic, not a margin."""
    calm = assess_mission(mission(weather=trace(speed_dmps=0)))
    blowing = assess_mission(mission(weather=trace(speed_dmps=40, bearing_mdeg=0)))

    assert blowing.estimated_consumption_wh != calm.estimated_consumption_wh
    assert blowing.terms[-1].wh == calm.terms[-1].wh == 0


def test_a_stale_frozen_snapshot_is_refused() -> None:
    """Expired weather is uncertainty, and uncertainty is not calm air.

    The clock used is the caller's integer scenario clock, never a wall clock.
    """
    stale = snapshot_from_rows(
        [(m, 30, DUE_EAST_MDEG, 30) for m in range(30)],
        station="IXC-synthetic",
        source="frozen test trace",
        bundle_sha256="a" * 64,
        retrieved_at_us=BASE_US,
        expires_at_us=BASE_US + 10 * MINUTE_US,
    )
    decided_at = BASE_US + 30 * MINUTE_US
    report = assess_mission(
        mission(distance_mm=3 * KM_MM, recovery_mm=3 * KM_MM, weather=stale, clock_us=decided_at)
    )

    assert stale.freshness(BASE_US) is Freshness.FRESH
    assert stale.freshness(decided_at) is Freshness.STALE
    assert not report.feasible
    assert RejectReason.STALE_WEATHER in report.reasons


def test_sustained_wind_above_the_airframe_limit_is_a_hard_no() -> None:
    """A demonstrated limit is not a ranking input: it blocks regardless of margin."""
    report = assess_mission(
        mission(distance_mm=1 * KM_MM, recovery_mm=1 * KM_MM, weather=trace(speed_dmps=79))
    )
    beyond = assess_mission(
        mission(distance_mm=1 * KM_MM, recovery_mm=1 * KM_MM, weather=trace(speed_dmps=90))
    )

    assert report.feasible
    assert not beyond.feasible
    assert RejectReason.SUSTAINED_WIND_ABOVE_AIRFRAME_LIMIT in beyond.reasons


# ------------------------------------------------------------------- battery
def test_degradation_crosses_the_feasibility_threshold_once() -> None:
    """An aging pack moves the margin down monotonically until the mission fails."""
    margins: list[tuple[int, bool]] = []
    for soh in range(10_000, 5_900, -500):
        report = assess_mission(mission(distance_mm=30 * KM_MM, recovery_mm=30 * KM_MM, soh_bp=soh))
        margins.append((soh, report.feasible))

    assert margins[0][1] is True, "a fresh pack flies this mission"
    assert margins[-1][1] is False, "a badly degraded one does not"
    # Once it has failed it cannot become feasible again by degrading further.
    seen_failure = False
    for _soh, feasible in margins:
        if not feasible:
            seen_failure = True
        elif seen_failure:
            pytest.fail(f"feasibility returned after degradation: {margins}")
    assert seen_failure


def test_lower_charge_lowers_available_energy_and_the_margin() -> None:
    high = assess_mission(mission(charge_bp=10_000))
    low = assess_mission(mission(charge_bp=4_000))

    assert high.available_wh > low.available_wh
    assert high.margin_wh > low.margin_wh
    assert high.reserve_required_wh == low.reserve_required_wh, "reserve is a pack fraction"


def test_heavier_airframe_holds_a_bigger_reserve_fraction() -> None:
    """Two committed profiles differ, and the difference is the profile's, not code."""
    near = mission(distance_mm=3 * KM_MM, recovery_mm=3 * KM_MM)
    x = assess_mission(replace(near, profile=AIRFRAME_X_SIM))
    y = assess_mission(replace(near, profile=AIRFRAME_Y_SIM))

    assert x.profile.reserve_fraction_bp < y.profile.reserve_fraction_bp
    assert x.terms[3].wh < y.terms[3].wh


# ------------------------------------------------------------------- margin
def test_margin_is_a_signed_integer_not_a_boolean() -> None:
    """Dispatch ranks on this number, so the size of the deficit has to survive."""
    feasible = assess_mission(mission(distance_mm=3 * KM_MM, recovery_mm=3 * KM_MM))
    infeasible = assess_mission(mission(distance_mm=3 * KM_MM, recovery_mm=150 * KM_MM))

    assert isinstance(feasible.margin_wh, int)
    assert isinstance(infeasible.margin_wh, int)
    assert infeasible.margin_wh < 0
    assert feasible.margin_wh > 0
    assert infeasible.summary.startswith("REJECTED")
    assert "margin=-" in infeasible.summary


def test_reports_rank_by_signed_margin_then_profile() -> None:
    near = assess_mission(mission(distance_mm=25 * KM_MM, recovery_mm=25 * KM_MM))
    comfortable = assess_mission(mission(distance_mm=3 * KM_MM, recovery_mm=3 * KM_MM))
    ranked = rank_reports([near, comfortable])

    assert ranked[0] is comfortable
    assert ranked[0].margin_wh >= ranked[1].margin_wh
    assert comfortable.rank_key() < near.rank_key()


def test_contract_estimate_only_exists_for_a_feasible_complete_mission() -> None:
    """``EnergyEstimate.margin_wh`` is non-negative by schema, so a deficit cannot pass."""
    feasible = assess_mission(mission(distance_mm=3 * KM_MM, recovery_mm=3 * KM_MM))
    estimate = feasible.to_energy_estimate()

    assert estimate.margin_wh == feasible.margin_wh
    assert estimate.estimated_consumption_wh == feasible.estimated_consumption_wh
    assert estimate.reserve_required_wh == feasible.reserve_required_wh
    assert AIRFRAME_X_SIM.name in estimate.profile_id
    assert feasible.profile_sha256[:12] in estimate.profile_id
    with pytest.raises(Exception, match="infeasible mission"):
        assess_mission(mission(recovery_mm=150 * KM_MM)).to_energy_estimate()


# ------------------------------------------------------------ geometry bridge
def test_a_geodesic_polyline_becomes_integer_legs_and_is_costed() -> None:
    """geom-core measures the route; the kernel only ever sees millimetres."""
    from droneops_energy import route_from_points
    from droneops_geom_core import GeoPoint

    hub_central = GeoPoint(30.7383, 76.7936)
    hub_north = GeoPoint(30.766, 76.785)
    polyline = (hub_central, GeoPoint(30.753323, 76.790895), hub_north)
    legs = route_from_points(
        "outbound", polyline, heights_mm=(320_000, 380_000, 350_000), hover_us=30_000_000
    )

    assert len(legs.legs) == 2
    assert all(leg.distance_mm > 0 and leg.distance_mm % 1 == 0 for leg in legs.legs)
    assert all(0 <= leg.course_mdeg < 360_000 for leg in legs.legs)
    assert legs.climb_mm == 60_000 and legs.descent_mm == 30_000
    assert legs.hover_us == 30_000_000
    report = assess_mission(
        replace(
            mission(distance_mm=3 * KM_MM, recovery_mm=3 * KM_MM),
            outbound=legs,
        )
    )
    assert report.outbound.energy_mwh > 0
    assert report.feasible


def test_a_degenerate_polyline_is_refused_not_costed_as_free() -> None:
    from droneops_energy import route_from_points
    from droneops_geom_core import GeoPoint

    same = GeoPoint(30.7383, 76.7936)
    with pytest.raises(Exception, match="zero-length segment"):
        route_from_points("outbound", (same, same))
    with pytest.raises(Exception, match="at least two points"):
        route_from_points("outbound", (same,))
    with pytest.raises(Exception, match="heights for"):
        route_from_points("outbound", (same, GeoPoint(30.74, 76.80)), heights_mm=(1,))


# ------------------------------------------------------------- versioned basis
def test_every_output_states_that_its_basis_is_illustrative() -> None:
    report = assess_mission(mission())

    assert report.disclaimer == ILLUSTRATIVE_DISCLAIMER
    assert report.profile.illustrative is True
    assert report.profile.authoritative is False
    assert report.profile.certification_status == "unverified-simulated"


def test_profiles_are_named_versioned_and_content_hashed() -> None:
    """The same mapping hashes the same; any change of a constant changes the pin."""
    again = load_profile(AIRFRAME_X_SIM.name, AIRFRAME_X_SIM.version)
    changed = publish_profile(
        {**AIRFRAME_X_SIM.to_mapping(), "cruise_power_w": str(AIRFRAME_X_SIM.cruise_power_w + 1)}
    )

    assert again.parameters_sha256 == AIRFRAME_X_SIM.parameters_sha256
    assert changed.parameters_sha256 != AIRFRAME_X_SIM.parameters_sha256
    assert again.verify_hash(AIRFRAME_X_SIM.parameters_sha256) is again
    with pytest.raises(Exception, match="edited in place"):
        again.verify_hash(changed.parameters_sha256)


def test_a_profile_version_cannot_be_loaded_by_guessing() -> None:
    with pytest.raises(UnknownProfileError, match="stored profiles"):
        load_profile("airframe-z", "no-such-version")


def test_a_profile_cannot_drop_its_illustrative_flag_without_claiming_authority() -> None:
    mapping = {**AIRFRAME_X_SIM.to_mapping(), "name": "airframe-x-pretend", "illustrative": "false"}
    with pytest.raises(Exception, match="may only drop the illustrative flag"):
        publish_profile(mapping)
    both = BatteryProfileVersion.from_mapping({**mapping, "authoritative": "true"})
    assert both.illustrative is False
    assert "authoritative profile" in both.basis


def test_unknown_profile_fields_are_refused_not_ignored() -> None:
    """A typo in a battery constant must not silently become 'the default'."""
    with pytest.raises(Exception, match="unknown profile fields"):
        publish_profile({**AIRFRAME_X_SIM.to_mapping(), "cruise_powr_w": "999"})
    with pytest.raises(Exception, match="missing fields"):
        publish_profile({"name": "incomplete"})


# ------------------------------------------------------------------ crosscheck
def test_battery_model_verdict_disagreement_is_never_accepted() -> None:
    """Failure model, section 19: require review or an alternate aircraft.

    The same physical mission priced by two committed profiles: airframe-x's smaller
    but more efficient pack flies 40 km out and back, airframe-y's does not. Neither
    number is trustworthy, so the mission goes to a human, not to dispatch.
    """
    shape = mission(
        profile=AIRFRAME_X_SIM,
        distance_mm=40 * KM_MM,
        recovery_mm=40 * KM_MM,
        payload_g=500,
    )
    outcome = compare_battery_models(shape, [AIRFRAME_X_SIM, AIRFRAME_Y_SIM])

    assert [row.feasible for row in outcome.comparisons] == [True, False]
    assert outcome.agreement is Agreement.DISAGREE_ON_VERDICT
    assert outcome.review_required is True
    assert outcome.accepted is False
    assert RejectReason.BATTERY_MODELS_DISAGREE in outcome.primary.reasons
    assert "Do not accept" in outcome.recommendation()
    assert outcome.spread_wh > 0
    assert "ILLUSTRATIVE" in outcome.disclaimer


def test_battery_models_agreeing_on_feasibility_but_far_apart_also_block() -> None:
    """A 24 Wh spread across two models, on a mission that "fits", is still a review.

    Both models say yes, so a naive pass/fail check would dispatch: the margin is
    what makes the disagreement visible, which is why the model returns a margin.
    """
    shape = mission(
        profile=AIRFRAME_Y_SIM, distance_mm=30 * KM_MM, recovery_mm=30 * KM_MM, payload_g=500
    )
    outcome = compare_battery_models(shape, [AIRFRAME_Y_SIM, AIRFRAME_X_SIM])

    assert all(row.feasible for row in outcome.comparisons)
    assert outcome.agreement is Agreement.DISAGREE_ON_MARGIN
    assert outcome.review_required is True
    assert outcome.accepted is False
    assert "tolerance" in outcome.recommendation()


def test_agreeing_models_are_accepted_and_the_tolerance_is_versioned() -> None:
    shape = mission(distance_mm=3 * KM_MM, recovery_mm=3 * KM_MM)
    outcome = compare_battery_models(
        shape,
        [AIRFRAME_X_SIM, AIRFRAME_X_SIM],
        policy=ReviewPolicy(policy_version="t-1", tolerance_wh=0),
    )

    assert outcome.agreement is Agreement.AGREE_FEASIBLE
    assert outcome.accepted is True
    assert outcome.spread_wh == 0
    assert outcome.policy_version == "t-1"
    with pytest.raises(Exception, match="cannot be negative"):
        ReviewPolicy(policy_version="bad", tolerance_wh=-1)
    with pytest.raises(Exception, match="illustrative"):
        ReviewPolicy(policy_version="bad", tolerance_wh=5, illustrative=False)


def test_a_model_that_cannot_carry_the_load_votes_infeasible() -> None:
    """A refusal to compute is not agreement: it is recorded as a divergence."""
    shape = mission(
        profile=AIRFRAME_Y_SIM, distance_mm=3 * KM_MM, recovery_mm=3 * KM_MM, payload_g=3000
    )
    outcome = compare_battery_models(shape, [AIRFRAME_Y_SIM, AIRFRAME_X_SIM])

    refused = [row for row in outcome.comparisons if not row.computed]
    assert refused, "airframe-x cannot lift 3000 g"
    assert all(row.feasible is False for row in refused)
    assert outcome.agreement is Agreement.DISAGREE_ON_VERDICT
