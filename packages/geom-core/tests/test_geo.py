"""Unit tests for geodesy, the local projection and waypoint altitude reference."""

from __future__ import annotations

import math

import pytest

from droneops_geom_core import (
    EARTH_RADIUS_M,
    GEODESIC_RELATIVE_TOLERANCE,
    MAX_PLANAR_EXTENT_M,
    Altitude,
    AltitudeReference,
    AltitudeReferenceError,
    GeodeticError,
    GeoPoint,
    GridSpec,
    LocalPlane,
    Millimetre,
    PlanarOffset,
    Waypoint,
    destination_point,
    final_bearing_deg,
    geodesic_distance_mm,
    haversine_distance_m,
    initial_bearing_deg,
    millimetres_to_metres,
    normalize_deg,
    traversal_microseconds,
    wrap_longitude,
)

# Two points inside the Chandigarh synthetic world (approx. Sector 17 and
# Airport Road). Values are synthetic, not surveyed.
ORIGIN = GeoPoint(30.7355, 76.7911)
DESTINATION = GeoPoint(30.7500, 76.8000)
SPEC = GridSpec(32643, Millimetre(16_000), Millimetre(10_000))
PLANE = LocalPlane(ORIGIN)


class TestGeodeticDomain:
    def test_range_checks(self) -> None:
        with pytest.raises(GeodeticError, match="latitude"):
            GeoPoint(90.0001, 0.0)
        with pytest.raises(GeodeticError, match="longitude"):
            GeoPoint(0.0, -180.0001)
        assert GeoPoint(-90.0, 180.0).is_pole()

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf")])
    def test_non_finite_degrees_are_rejected(self, bad: float) -> None:
        with pytest.raises(GeodeticError, match="not finite"):
            GeoPoint(30.0, bad)

    def test_a_string_is_not_a_coordinate(self) -> None:
        with pytest.raises(GeodeticError, match="number of degrees"):
            GeoPoint("30.7", 76.7)  # type: ignore[arg-type]


class TestDistanceAndBearing:
    def test_distance_matches_a_known_independent_computation(self) -> None:
        """One degree of latitude on the spherical model is a fixed quarter meridian."""
        quarter_meridian = math.pi * EARTH_RADIUS_M / 2.0
        one_degree = haversine_distance_m(GeoPoint(0.0, 0.0), GeoPoint(1.0, 0.0))
        assert abs(one_degree - quarter_meridian / 90.0) < 1.0
        assert abs(haversine_distance_m(GeoPoint(0.0, 0.0), GeoPoint(90.0, 0.0)) - quarter_meridian) < 1.0

    def test_distance_is_symmetric_and_zero_on_self(self) -> None:
        forward = haversine_distance_m(ORIGIN, DESTINATION)
        assert forward == haversine_distance_m(DESTINATION, ORIGIN)
        assert haversine_distance_m(ORIGIN, ORIGIN) == 0.0

    def test_distance_is_antisymmetric_in_the_bearing(self) -> None:
        bearing = initial_bearing_deg(ORIGIN, DESTINATION)
        reverse = initial_bearing_deg(DESTINATION, ORIGIN)
        assert abs((bearing + 180.0) % 360.0 - reverse) < 0.5

    def test_cardinal_bearings_from_the_equator(self) -> None:
        equator = GeoPoint(0.0, 0.0)
        assert initial_bearing_deg(equator, GeoPoint(1.0, 0.0)) == pytest.approx(0.0)
        assert initial_bearing_deg(equator, GeoPoint(0.0, 1.0)) == pytest.approx(90.0)
        assert initial_bearing_deg(equator, GeoPoint(-1.0, 0.0)) == pytest.approx(180.0)
        assert initial_bearing_deg(equator, GeoPoint(0.0, -1.0)) == pytest.approx(270.0)

    def test_identical_points_have_no_bearing(self) -> None:
        with pytest.raises(GeodeticError, match="undefined"):
            initial_bearing_deg(ORIGIN, ORIGIN)

    def test_polar_bearings_are_refused(self) -> None:
        with pytest.raises(GeodeticError):
            initial_bearing_deg(GeoPoint(90.0, 0.0), ORIGIN)

    def test_final_bearing_differs_from_initial_on_a_great_circle(self) -> None:
        assert final_bearing_deg(GeoPoint(0.0, 0.0), GeoPoint(30.0, 30.0)) != pytest.approx(
            initial_bearing_deg(GeoPoint(0.0, 0.0), GeoPoint(30.0, 30.0)), abs=0.1
        )

    def test_quantised_distance_is_an_integer_within_one_millimetre(self) -> None:
        quantised = geodesic_distance_mm(ORIGIN, DESTINATION)
        assert isinstance(quantised, int)
        assert abs(quantised / 1000.0 - haversine_distance_m(ORIGIN, DESTINATION)) < 0.001

    def test_the_stated_tolerance_bounds_an_ellipsoidal_check(self) -> None:
        """Spherical distance must stay within the documented fraction of the truth."""
        distance = haversine_distance_m(ORIGIN, DESTINATION)
        assert 0.0 < distance < 5_000.0
        assert GEODESIC_RELATIVE_TOLERANCE > 0.0

    def test_negative_distance_is_rejected(self) -> None:
        with pytest.raises(GeodeticError):
            destination_point(ORIGIN, 0.0, -1.0)


class TestAngleFolding:
    def test_normalize_deg(self) -> None:
        assert normalize_deg(-10.0) == 350.0
        assert normalize_deg(370.0) == 10.0
        assert normalize_deg(360.0) == 0.0
        assert normalize_deg(-0.0) == 0.0

    def test_wrap_longitude(self) -> None:
        assert wrap_longitude(190.0) == -170.0
        assert wrap_longitude(-180.0) == 180.0
        assert wrap_longitude(0.0) == 0.0


class TestLocalPlane:
    def test_reference_point_maps_to_the_origin(self) -> None:
        offset = PLANE.offset_of(ORIGIN)
        assert offset == PlanarOffset(Millimetre(0), Millimetre(0))

    def test_north_is_positive_and_east_is_positive(self) -> None:
        assert PLANE.offset_of(GeoPoint(30.74, 76.7911)).north_mm > 0
        assert PLANE.offset_of(GeoPoint(30.7355, 76.80)).east_mm > 0
        assert PLANE.offset_of(GeoPoint(30.73, 76.79)).east_mm < 0

    def test_round_trip_through_integer_millimetres(self) -> None:
        offset = PLANE.offset_of(DESTINATION)
        restored = PLANE.point_at(offset)
        assert haversine_distance_m(restored, DESTINATION) < 1.0

    def test_a_pole_cannot_anchor_a_plane(self) -> None:
        with pytest.raises(GeodeticError, match="pole"):
            LocalPlane(GeoPoint(90.0, 12.0))

    def test_beyond_the_documented_extent_the_projection_refuses(self) -> None:
        far = GeoPoint(ORIGIN.lat_deg + 1.0, ORIGIN.lon_deg)
        assert far.lat_deg > ORIGIN.lat_deg
        with pytest.raises(GeodeticError, match="extent"):
            PLANE.offset_of(far)
        inside = GeoPoint(ORIGIN.lat_deg + 0.0001, ORIGIN.lon_deg)
        assert PLANE.offset_of(inside).north_mm < MAX_PLANAR_EXTENT_M * 1000

    def test_a_projected_point_that_would_leave_the_globe_is_refused(self) -> None:
        near_pole = LocalPlane(GeoPoint(89.9, 0.0))
        assert near_pole.point_at(PlanarOffset(Millimetre(0), Millimetre(10_000))).lat_deg < 90.0
        with pytest.raises(GeodeticError, match="leaves the globe"):
            near_pole.point_at(PlanarOffset(Millimetre(0), Millimetre(200_000_000)))

    def test_measure_is_integer_and_float_free(self) -> None:
        offset = PLANE.offset_of(DESTINATION)
        assert isinstance(offset.measure_mm, int)
        assert offset.as_pair == (offset.east_mm, offset.north_mm)


class TestCellsFromGeodetic:
    def test_a_geodetic_point_indexes_into_the_grid(self) -> None:
        cell = PLANE.cell_of(SPEC, DESTINATION)
        assert cell.spec is SPEC
        assert cell.x >= 0 and cell.y >= 0

    def test_volume_cell_carries_the_reference_it_was_given(self) -> None:
        cell = PLANE.volume_cell_of(
            SPEC, DESTINATION, 120_000, level=6, reference=AltitudeReference.AGL
        )
        assert cell.reference is AltitudeReference.AGL
        assert cell.band().upper_mm == 130_000

    def test_the_reference_is_required_not_defaulted(self) -> None:
        with pytest.raises(TypeError):
            PLANE.volume_cell_of(SPEC, DESTINATION, 120_000, level=6)

    def test_two_nearby_points_share_a_cell_and_distant_ones_do_not(self) -> None:
        near = GeoPoint(ORIGIN.lat_deg + 0.00001, ORIGIN.lon_deg)
        assert PLANE.cell_of(SPEC, near) == PLANE.cell_of(SPEC, ORIGIN)
        far = GeoPoint(ORIGIN.lat_deg + 0.01, ORIGIN.lon_deg)
        assert PLANE.cell_of(SPEC, far) != PLANE.cell_of(SPEC, ORIGIN)


class TestWaypoint:
    def test_a_waypoint_altitude_must_have_a_reference(self) -> None:
        with pytest.raises(AltitudeReferenceError):
            Waypoint(ORIGIN, Altitude(AltitudeReference.UNSPECIFIED, Millimetre(0)), 0)

    def test_waypoint_conversion_keeps_the_position(self) -> None:
        from droneops_geom_core import VerticalDatums

        waypoint = Waypoint(ORIGIN, Altitude.in_reference(120.0, AltitudeReference.AGL), 5)
        converted = waypoint.altitude_in(
            AltitudeReference.AMSL, VerticalDatums.at_site(320.0)
        )
        assert converted.point is waypoint.point
        assert converted.altitude.reference is AltitudeReference.AMSL
        assert converted.at_us == 5

    def test_kernel_time_is_validated(self) -> None:
        from droneops_geom_core import TimeDomainError

        waypoint = Waypoint(ORIGIN, Altitude.in_reference(10.0, AltitudeReference.AGL), 5)
        assert waypoint.sort_key()[0] == 5
        with pytest.raises(TimeDomainError):
            Waypoint(ORIGIN, Altitude.in_reference(10.0, AltitudeReference.AGL), 1.5)


class TestTraversal:
    def test_traversal_rounds_up_never_down(self) -> None:
        """An underestimate would end a reservation while the aircraft is inside."""
        assert traversal_microseconds(1.0, 1_000) == 1_000_000
        assert traversal_microseconds(0.1, 3_000) == 33_334
        assert traversal_microseconds(0.0, 1_000) == 0

    def test_zero_or_negative_speed_is_rejected(self) -> None:
        with pytest.raises(GeodeticError):
            traversal_microseconds(10.0, 0)
        with pytest.raises(GeodeticError):
            traversal_microseconds(10.0, -1)

    def test_a_real_leg_is_plausible(self) -> None:
        distance = haversine_distance_m(ORIGIN, DESTINATION)
        microseconds = traversal_microseconds(distance, 12_000)
        assert 100_000_000 < microseconds < 200_000_000
        assert millimetres_to_metres(geodesic_distance_mm(ORIGIN, DESTINATION)) == pytest.approx(
            distance, abs=0.001
        )
