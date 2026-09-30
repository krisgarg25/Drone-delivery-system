"""``RouteProfile``: monotonic time, one altitude frame, interpolation to any instant.

These tests are about the *law*, not the geometry it produces: what a profile
refuses to be, and what it says about the body at an instant nobody sampled.
"""

from __future__ import annotations

import math
from fractions import Fraction

import pytest
from geom4d_scenarios import ORIGIN, straight_profile, two_leg_profile

from droneops_contracts.geometry import AltitudeReference as ContractReference
from droneops_contracts.geometry import Position
from droneops_contracts.route import CoordinateTimeProfile, RoutePoint
from droneops_geom_4d import (
    MAX_POINTS,
    MIN_POINTS,
    ProfileError,
    ProfilePoint,
    RouteProfile,
    exact_axis,
    quantised_axis,
)
from droneops_geom_core import (
    MICROS_PER_SECOND,
    AltitudeReference,
    GeoPoint,
    Interval,
    Microsecond,
    destination_point,
    metres_to_millimetres,
)

pytestmark = pytest.mark.unit

AGL = AltitudeReference.AGL
AMSL = AltitudeReference.AMSL


def leg(
    distance_m: float,
    bearing_deg: float,
    *,
    at_us: int,
    altitude_m: float = 40.0,
    speed_mmps: int = 10_000,
) -> ProfilePoint:
    """One waypoint away from the origin."""
    point = destination_point(ORIGIN, bearing_deg, distance_m)
    return ProfilePoint.at(
        at_us, point.lat_deg, point.lon_deg, metres_to_millimetres(altitude_m), AGL, speed_mmps
    )


def test_a_straight_leg_is_exactly_two_waypoints_apart_in_time() -> None:
    profile = straight_profile(distance_m=100.0, bearing_deg=90.0, duration_s=10)
    assert profile.window == Interval.of(0, 10 * MICROS_PER_SECOND)
    assert profile.duration_us == Microsecond(10 * MICROS_PER_SECOND)
    assert len(profile.segments()) == 1
    assert profile.segments()[0].window == profile.window


def test_monotonic_time_is_enforced_not_repaired() -> None:
    """A repeated timestamp would interpolate a place the body never was."""
    same = [leg(0, 90, at_us=5), leg(100, 90, at_us=5)]
    with pytest.raises(ProfileError, match="strictly monotonic"):
        RouteProfile.from_points(same, plan_version="p", source="s")
    reversed_leg = [leg(100, 90, at_us=10), leg(0, 90, at_us=5)]
    with pytest.raises(ProfileError, match="strictly monotonic"):
        RouteProfile.from_points(reversed_leg, plan_version="p", source="s")


def test_one_waypoint_cannot_describe_motion() -> None:
    with pytest.raises(ProfileError, match=str(MIN_POINTS)):
        RouteProfile.from_points([leg(0, 90, at_us=0)], plan_version="p", source="s")


def test_an_absurd_profile_size_is_refused_before_it_is_swept() -> None:
    points = [leg(0.0, 90, at_us=index * 1_000_000) for index in range(MAX_POINTS + 1)]
    with pytest.raises(ProfileError, match="ceiling"):
        RouteProfile.from_points(points, plan_version="p", source="s")


def test_mixing_altitude_references_is_refused() -> None:
    """I9: the datum difference is exactly the thing that would have to be guessed."""
    lower = ProfilePoint.at(0, ORIGIN.lat_deg, ORIGIN.lon_deg, 40_000, AGL, 10_000)
    higher = ProfilePoint.at(1_000_000, ORIGIN.lat_deg, ORIGIN.lon_deg, 60_000, AMSL, 10_000)
    with pytest.raises(ProfileError, match="one altitude reference"):
        RouteProfile.from_points([lower, higher], plan_version="p", source="s")


def test_a_profile_must_stay_in_one_plane_of_origin() -> None:
    profile = straight_profile(distance_m=50.0, bearing_deg=90.0)
    assert profile.reference is AGL
    assert profile.plan_version
    assert profile.source
    assert profile.authoritative is False


def test_interpolation_reaches_an_instant_nobody_sampled() -> None:
    """Halfway along a 100 m east leg the body is 50 m east of the origin."""
    profile = straight_profile(distance_m=100.0, bearing_deg=90.0, duration_s=10)
    state = profile.state_at(5 * MICROS_PER_SECOND)
    assert abs(state.east_mm - 50_000) <= 5
    assert abs(state.north_mm) <= 5
    assert state.altitude_mm == 40_000
    assert state.reference is AGL


def test_interpolation_is_exact_at_waypoints_and_rational_everywhere_else() -> None:

    assert exact_axis(5, 0, 10, 0, 100) == 50
    assert exact_axis(1, 0, 3, 0, 1) == Fraction(1, 3)
    assert quantised_axis(1, 0, 3, 0, 1) == 0
    # clamped outside: the volume exists where the plan said it would be
    assert exact_axis(-7, 0, 10, 3, 13) == 3
    assert exact_axis(99, 0, 10, 3, 13) == 13


def test_querying_outside_the_plan_is_a_rejection() -> None:
    """Interpolating past the end would extend a volume the plan never reserved."""
    profile = straight_profile(distance_m=100.0, bearing_deg=90.0, duration_s=10)
    with pytest.raises(ProfileError, match="outside the profile window"):
        profile.state_at(11 * MICROS_PER_SECOND)
    assert profile.state_at(10 * MICROS_PER_SECOND).altitude_mm == 40_000


def test_altitude_interpolates_linearly_along_a_climb() -> None:
    profile = two_leg_profile(
        first_m=40.0,
        second_m=40.0,
        bearing_deg=90.0,
        turn_bearing_deg=90.0,
        start_altitude_m=40.0,
        end_altitude_m=60.0,
        duration_s=10,
    )
    assert len(profile.segments()) == 2
    assert profile.state_at(15 * MICROS_PER_SECOND).altitude_mm == 50_000
    assert profile.altitude_band.lower_mm == 40_000
    assert profile.altitude_band.upper_mm == 60_000
    assert profile.segments()[1].vertical_rate_mm_per_us > 0
    assert profile.segments()[0].is_vertical_change is False
    assert profile.segments()[1].is_vertical_change is True


def test_declared_speed_that_its_own_waypoints_deny_is_refused() -> None:
    """A body cannot be stationary and move 100 m at the same time."""
    still = [leg(0, 90, at_us=0, speed_mmps=0), leg(100, 90, at_us=MICROS_PER_SECOND, speed_mmps=0)]
    with pytest.raises(ProfileError, match="declares zero speed"):
        RouteProfile.from_points(still, plan_version="p", source="s")


def test_negative_speed_is_refused() -> None:
    with pytest.raises(ProfileError, match="negative"):
        ProfilePoint.at(0, ORIGIN.lat_deg, ORIGIN.lon_deg, 40_000, AGL, -1)


def test_speed_bound_is_the_implied_chord_not_the_declaration() -> None:
    profile = straight_profile(distance_m=100.0, bearing_deg=90.0, duration_s=10, speed_mmps=10_000)
    assert math.isclose(float(profile.max_speed_mm_per_us), 0.01, rel_tol=1e-6)
    assert profile.max_vertical_rate_mm_per_us == 0


def test_footprint_offsets_start_at_the_origin_and_end_at_the_last_waypoint() -> None:
    profile = straight_profile(distance_m=100.0, bearing_deg=90.0, duration_s=10)
    offsets = profile.footprint_offsets()
    assert offsets[0] == (0, 0)
    assert abs(offsets[-1][0] - 100_000) <= 5
    assert len(offsets) == len(profile.segments()) + 1


def test_a_contract_route_profile_adopts_without_losing_its_units() -> None:
    """The published shape stays the interface; the kernel clock stays integer microseconds."""
    contract = CoordinateTimeProfile(
        points=[
            RoutePoint(
                t=0,
                position=Position(
                    lat=ORIGIN.lat_deg,
                    lon=ORIGIN.lon_deg,
                    altitude_m=40.0,
                    altitude_reference=ContractReference.AGL,
                ),
                speed_ms=10.0,
            ),
            RoutePoint(
                t=10,
                position=Position(
                    lat=ORIGIN.lat_deg,
                    lon=ORIGIN.lon_deg + 0.001157,
                    altitude_m=40.0,
                    altitude_reference=ContractReference.AGL,
                ),
                speed_ms=10.0,
            ),
        ]
    )
    profile = RouteProfile.from_contract_profile(
        contract, start_us=2 * MICROS_PER_SECOND, plan_version="plan-1", source="contract-test"
    )
    assert profile.window == Interval.of(2 * MICROS_PER_SECOND, 12 * MICROS_PER_SECOND)
    assert profile.points[0].speed_mmps == 10_000
    assert profile.reference is AGL


def test_a_position_without_a_reference_never_becomes_a_profile() -> None:
    """I9 is enforced by the contract type itself; the adapter cannot route around it."""
    with pytest.raises(ValueError, match="UNSPECIFIED"):
        Position(
            lat=ORIGIN.lat_deg,
            lon=ORIGIN.lon_deg,
            altitude_m=40.0,
            altitude_reference=ContractReference.UNSPECIFIED,
        )


def test_geodetic_position_round_trips_through_the_profile_plane() -> None:
    profile = straight_profile(distance_m=100.0, bearing_deg=90.0, duration_s=10)
    point = profile.position_at(5 * MICROS_PER_SECOND)
    assert isinstance(point, GeoPoint)
    assert math.isclose(point.lat_deg, ORIGIN.lat_deg, abs_tol=1e-6)
    assert point.lon_deg > ORIGIN.lon_deg
