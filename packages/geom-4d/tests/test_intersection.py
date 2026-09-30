"""Intersection, envelopes, interactions and the cheap pre-filter.

Volume-versus-volume, profile-versus-profile, the conservative discretised path
reconciled against the continuous one, and the cell-and-time-window index that has
to be a superset of every real conflict.

What these do not prove: that any particular margin is sufficient separation. They
prove that the arithmetic answers the question the margins define.
"""

from __future__ import annotations

import pytest
from geom4d_scenarios import (
    ORIGIN,
    PLANE,
    plan,
    point_at,
    straight_profile,
    two_leg_profile,
    waypoint,
)

from droneops_geom_4d import (
    InteractionKind,
    Manoeuvre,
    ProtectedBody,
    ProtectedVolumePolicy,
    SweepBasis,
    Verdict,
    VerdictCause,
    climb_descent_envelopes,
    conflict_verdict,
    discretised_conflict_verdict,
    frame_conflict_verdict,
    index_volumes,
    layer_occupancies,
    merge_and_crossing_volumes,
    pair_verdict,
    prefiltered_pair_verdicts,
    profile_verdict,
    static_volume,
    sweep_profile,
    volume_cells,
    volume_pair_verdict,
)
from droneops_geom_core import MICROS_PER_SECOND, AltitudeReference, Interval, LocalPlane

pytestmark = pytest.mark.unit

RADIUS_MM = 150 + 15_000 + 5_000  # body + horizontal margin + position uncertainty
VERTICAL_HALF_MM = 100 + 10_000  # body half height + vertical margin
CONTACT_AT_MM = 2 * RADIUS_MM  # centre distance at which two protected volumes touch


def head_on(policy, body: ProtectedBody) -> tuple:
    """The same lane, flown in both directions, over the same ten seconds."""
    west = point_at(270.0, 100.0)
    east = point_at(90.0, 100.0)
    outbound = plan(
        [
            waypoint(west, at_us=0, altitude_m=40.0, speed_mmps=40_000),
            waypoint(east, at_us=10 * MICROS_PER_SECOND, altitude_m=40.0, speed_mmps=40_000),
        ],
        plan_version="outbound",
        source="a",
    )
    inbound = plan(
        [
            waypoint(east, at_us=0, altitude_m=40.0, speed_mmps=40_000),
            waypoint(west, at_us=10 * MICROS_PER_SECOND, altitude_m=40.0, speed_mmps=40_000),
        ],
        plan_version="inbound",
        source="b",
    )
    return outbound, inbound


def test_head_on_traffic_on_one_lane_is_a_conflict(policy, body: ProtectedBody) -> None:
    outbound, inbound = head_on(policy, body)
    result = conflict_verdict(outbound, inbound, body, policy)
    assert result.verdict is Verdict.CONFLICT
    assert result.cause is VerdictCause.CONTACT
    assert len(result.windows) == 1
    # they meet near the middle of the lane, mid-way through the ten seconds
    contact = result.contacts[0]
    assert contact.at_us == result.windows[0].start
    # first touch is exactly one protected reach before the meeting point
    assert abs(contact.east_mm) <= RADIUS_MM + 2_000
    assert result.basis is SweepBasis.CONTINUOUS


def test_a_verdict_does_not_depend_on_which_side_asked(policy, body: ProtectedBody) -> None:
    outbound, inbound = head_on(policy, body)
    forward = conflict_verdict(outbound, inbound, body, policy)
    backward = conflict_verdict(inbound, outbound, body, policy)
    assert forward.verdict is backward.verdict
    assert forward.windows == backward.windows


def test_the_same_lane_at_different_times_is_clear(policy, body: ProtectedBody) -> None:
    early = straight_profile(distance_m=100.0, bearing_deg=90.0, duration_s=10)
    late = straight_profile(
        distance_m=100.0,
        bearing_deg=90.0,
        duration_s=10,
        start_us=30 * MICROS_PER_SECOND,
        plan_version="plan-late",
        source="b",
    )
    result = conflict_verdict(early, late, body, policy)
    assert result.verdict is Verdict.SAFE
    assert result.cause is VerdictCause.TIME_DISJOINT
    assert result.windows == ()


def test_vertical_separation_beyond_the_sum_of_margins_is_clear(
    policy, body: ProtectedBody
) -> None:
    """More than two vertical reaches apart, on the same track, at the same time."""
    low = straight_profile(distance_m=100.0, bearing_deg=90.0)
    high = straight_profile(
        distance_m=100.0,
        bearing_deg=90.0,
        altitude_m=40.0 + (2 * VERTICAL_HALF_MM + 1_000) / 1000.0,
        plan_version="plan-high",
        source="b",
    )
    result = conflict_verdict(low, high, body, policy)
    assert result.verdict is Verdict.SAFE
    assert result.cause is VerdictCause.VERTICAL_CLEAR


def test_lateral_separation_beyond_the_sum_of_radii_is_clear(policy, body: ProtectedBody) -> None:
    north = point_at(0.0, 100.0)
    south = point_at(180.0, 100.0)
    left = plan(
        [
            waypoint(south, at_us=0, altitude_m=40.0, speed_mmps=20_000),
            waypoint(north, at_us=20 * MICROS_PER_SECOND, altitude_m=40.0, speed_mmps=20_000),
        ],
        plan_version="lane-left",
        source="a",
    )
    right = plan(
        [
            waypoint(
                point_at(0.0, 100.0, from_point=point_at(90.0, CONTACT_AT_MM / 1000.0 + 30.0)),
                at_us=0,
                altitude_m=40.0,
                speed_mmps=20_000,
            ),
            waypoint(
                point_at(180.0, 100.0, from_point=point_at(90.0, CONTACT_AT_MM / 1000.0 + 30.0)),
                at_us=20 * MICROS_PER_SECOND,
                altitude_m=40.0,
                speed_mmps=20_000,
            ),
        ],
        plan_version="lane-right",
        source="b",
    )
    result = conflict_verdict(left, right, body, policy)
    assert result.verdict is Verdict.SAFE
    assert result.cause is VerdictCause.HORIZONTAL_CLEAR


def test_prisms_projected_on_different_planes_cannot_be_cleared(
    policy, body: ProtectedBody
) -> None:
    """Different local planes mean different numbers for the same ground."""
    mine = static_volume(
        policy=policy,
        body=body,
        plane=PLANE,
        source="a",
        plan_version="pad-a",
        east_mm=0,
        north_mm=0,
        altitude_mm=40_000,
        reference=AltitudeReference.AGL,
        from_us=0,
        to_us=MICROS_PER_SECOND,
    )
    theirs = static_volume(
        policy=policy,
        body=body,
        plane=LocalPlane(point_at(90.0, 1.0)),
        source="b",
        plan_version="pad-b",
        east_mm=0,
        north_mm=0,
        altitude_mm=40_000,
        reference=AltitudeReference.AGL,
        from_us=0,
        to_us=MICROS_PER_SECOND,
    )
    assert pair_verdict(mine, theirs).verdict is Verdict.UNPROVEN
    assert volume_pair_verdict(mine, theirs).verdict is Verdict.UNPROVEN


def test_mixed_altitude_frames_are_unproven_never_safe(policy, body: ProtectedBody) -> None:
    """An AGL height and an AMSL height need site datums, and this kernel will not guess."""
    agl = static_volume(
        policy=policy,
        body=body,
        plane=PLANE,
        source="a",
        plan_version="agl",
        east_mm=0,
        north_mm=0,
        altitude_mm=40_000,
        reference=AltitudeReference.AGL,
        from_us=0,
        to_us=MICROS_PER_SECOND,
    )
    amsl_policy = ProtectedVolumePolicy(
        schema_version=policy.schema_version,
        policy_version="amsl-experiment",
        authoritative=False,
        altitude_reference=AltitudeReference.AMSL,
        horizontal_margin_mm=policy.horizontal_margin_mm,
        vertical_margin_mm=policy.vertical_margin_mm,
        position_uncertainty_mm=policy.position_uncertainty_mm,
        timing_uncertainty_us=policy.timing_uncertainty_us,
        transition_buffer_us=policy.transition_buffer_us,
    )
    amsl = static_volume(
        policy=amsl_policy,
        body=body,
        plane=PLANE,
        source="b",
        plan_version="amsl",
        east_mm=0,
        north_mm=0,
        altitude_mm=40_000,
        reference=AltitudeReference.AMSL,
        from_us=0,
        to_us=MICROS_PER_SECOND,
    )
    result = pair_verdict(agl, amsl)
    assert result.verdict is Verdict.UNPROVEN
    assert result.cause is VerdictCause.REFERENCE_MISMATCH
    assert profile_verdict([agl], [amsl]).verdict is Verdict.UNPROVEN


def test_the_conservative_volume_test_never_misses_a_real_contact(
    policy, body: ProtectedBody
) -> None:
    """Exact sweep says contact => the footprint/band/window test does not say safe."""
    outbound, inbound = head_on(policy, body)
    left = sweep_profile(outbound, body, policy)
    right = sweep_profile(inbound, body, policy)
    for a in left:
        for b in right:
            exact = pair_verdict(a, b)
            conservative = volume_pair_verdict(a, b)
            if exact.verdict is Verdict.CONFLICT:
                assert conservative.verdict is Verdict.CONFLICT
                assert conservative.windows[0].start <= exact.windows[0].start
                assert conservative.windows[0].end >= exact.windows[-1].end


def test_discretised_path_agrees_with_the_continuous_one(policy, body: ProtectedBody) -> None:
    """Containment in both directions, for a range of steps.

    ``CONFLICT`` continuous always survives discretisation; ``SAFE`` discretised
    always means safe continuous. The only permitted disagreement is a discretised
    ``CONFLICT`` the continuous sweep later explains away - the inflation talking.
    """
    outbound, inbound = head_on(policy, body)
    level = straight_profile(distance_m=100.0, bearing_deg=90.0)
    high = straight_profile(
        distance_m=100.0, bearing_deg=90.0, altitude_m=90.0, plan_version="high", source="b"
    )
    for step_us in (10 * MICROS_PER_SECOND, 2 * MICROS_PER_SECOND, 500_000, 50_000):
        exact = conflict_verdict(outbound, inbound, body, policy)
        approx = discretised_conflict_verdict(outbound, inbound, body, policy, step_us)
        assert exact.verdict is Verdict.CONFLICT
        assert approx.verdict is Verdict.CONFLICT, step_us
        clear_exact = conflict_verdict(level, high, body, policy)
        clear_approx = discretised_conflict_verdict(level, high, body, policy, step_us)
        assert clear_exact.verdict is Verdict.SAFE
        assert clear_approx.verdict is Verdict.SAFE, step_us
    fine = discretised_conflict_verdict(outbound, inbound, body, policy, 50_000)
    coarse = discretised_conflict_verdict(outbound, inbound, body, policy, 2 * MICROS_PER_SECOND)
    assert fine.verdict is coarse.verdict


def test_a_one_hz_frame_check_misses_the_meeting_entirely(policy, body: ProtectedBody) -> None:
    """The reconciliation result that matters: frames can be safe while time is not."""
    outbound, inbound = head_on(policy, body)
    frames = frame_conflict_verdict(outbound, inbound, body, policy, 10 * MICROS_PER_SECOND)
    assert frames.verdict is Verdict.SAFE
    assert frames.basis is SweepBasis.FRAME_SAMPLE
    assert conflict_verdict(outbound, inbound, body, policy).verdict is Verdict.CONFLICT


def test_a_climb_gets_a_transition_buffer_and_a_footprint_that_reaches(
    policy, body: ProtectedBody
) -> None:
    profile = two_leg_profile(
        first_m=60.0,
        second_m=60.0,
        bearing_deg=90.0,
        turn_bearing_deg=90.0,
        start_altitude_m=30.0,
        end_altitude_m=70.0,
        duration_s=10,
        speed_mmps=6_000,
    )
    envelopes = climb_descent_envelopes(profile, body, policy)
    assert len(envelopes) == 1
    envelope = envelopes[0]
    assert envelope.manoeuvre is Manoeuvre.CLIMB
    assert envelope.transition_buffer_us == policy.transition_buffer_us
    assert (
        envelope.window.start
        == 10 * MICROS_PER_SECOND - policy.timing_uncertainty_us - policy.transition_buffer_us
    )
    # the body can still travel during the buffer, so the footprint is wider than the leg
    assert (
        envelope.along_track_extension_mm
        == 6_000 * policy.transition_buffer_us // MICROS_PER_SECOND
        + (1 if 6_000 * policy.transition_buffer_us % MICROS_PER_SECOND else 0)
    )
    assert envelope.band.lower_mm == 30_000 - VERTICAL_HALF_MM


def test_layer_occupancies_use_the_caller_grid_and_cover_the_manoeuvre(policy, body, grid) -> None:
    profile = two_leg_profile(
        first_m=40.0,
        second_m=40.0,
        bearing_deg=90.0,
        turn_bearing_deg=90.0,
        start_altitude_m=30.0,
        end_altitude_m=70.0,
        duration_s=10,
        speed_mmps=6_000,
    )
    occupancies = layer_occupancies(profile, body, policy, grid.vertical_band_mm)
    assert occupancies
    for occupancy in occupancies:
        assert occupancy.layer.lower_mm % grid.vertical_band_mm == 0
        assert occupancy.layer.measure == grid.vertical_band_mm
        assert occupancy.window.end > occupancy.window.start
    # the climb's protected extent spans 30-10 m to 70+10 m, so it crosses several layers
    assert len(occupancies) >= 8
    assert occupancies == tuple(sorted(occupancies, key=lambda item: item.occupancy_id.__str__()))


def test_merge_and_crossing_volumes_are_generated_from_the_sweep(
    policy, body: ProtectedBody
) -> None:
    outbound, inbound = head_on(policy, body)
    crossings = merge_and_crossing_volumes(
        sweep_profile(outbound, body, policy), sweep_profile(inbound, body, policy), policy
    )
    assert crossings
    assert all(volume.kind is InteractionKind.MERGE for volume in crossings)
    for volume in crossings:
        assert volume.window.end >= volume.window.start
        assert volume.transition_buffer_us == policy.transition_buffer_us

    north = point_at(0.0, 200.0)
    south = point_at(180.0, 200.0)
    east = point_at(90.0, 200.0)
    west = point_at(270.0, 200.0)
    vertical = plan(
        [
            waypoint(south, at_us=0, altitude_m=40.0, speed_mmps=30_000),
            waypoint(north, at_us=20 * MICROS_PER_SECOND, altitude_m=40.0, speed_mmps=30_000),
        ],
        plan_version="northbound",
        source="c",
    )
    horizontal = plan(
        [
            waypoint(west, at_us=0, altitude_m=40.0, speed_mmps=30_000),
            waypoint(east, at_us=20 * MICROS_PER_SECOND, altitude_m=40.0, speed_mmps=30_000),
        ],
        plan_version="eastbound",
        source="d",
    )
    crossing = merge_and_crossing_volumes(
        sweep_profile(vertical, body, policy), sweep_profile(horizontal, body, policy), policy
    )
    assert crossing
    assert {volume.kind for volume in crossing} == {InteractionKind.CROSSING}


def test_the_cell_prefilter_never_drops_a_real_conflict(policy, body, grid) -> None:
    outbound, inbound = head_on(policy, body)
    left = sweep_profile(outbound, body, policy)
    right = sweep_profile(inbound, body, policy)
    left_index = index_volumes(left, grid, level=1, max_cells=4_000)
    right_index = index_volumes(right, grid, level=1, max_cells=4_000)
    candidates = set(left_index.candidates(right_index))
    exact = {
        (a.volume_id, b.volume_id)
        for a in left
        for b in right
        if pair_verdict(a, b).verdict is Verdict.CONFLICT
    }
    assert exact
    assert exact <= candidates
    narrowed = prefiltered_pair_verdicts(left, right, grid=grid, level=1, max_cells=4_000)
    found = {(a_id, b_id) for a_id, b_id, result in narrowed if result.verdict is Verdict.CONFLICT}
    assert found == exact
    assert profile_verdict(left, right).verdict is Verdict.CONFLICT


def test_cells_cover_the_footprint_and_the_band(policy, body, grid) -> None:
    prism = sweep_profile(straight_profile(distance_m=100.0, bearing_deg=90.0), body, policy)[0]
    cells = volume_cells(prism, grid, level=1, max_cells=4_000)
    assert cells
    assert all(cell.reference is prism.reference for cell in cells)
    band = prism.band()
    for cell in cells:
        assert cell.band().upper_mm > band.lower_mm
        assert cell.band().lower_mm < band.upper_mm
    with pytest.raises(Exception, match="rasterises onto more than"):
        volume_cells(prism, grid, level=1, max_cells=1)


def test_index_is_reusable_and_deterministic(policy, body, grid) -> None:
    profile = straight_profile(distance_m=100.0, bearing_deg=90.0)
    first = index_volumes(sweep_profile(profile, body, policy), grid, level=1, max_cells=4_000)
    second = index_volumes(sweep_profile(profile, body, policy), grid, level=1, max_cells=4_000)
    assert first.buckets == second.buckets
    assert first.volumes == second.volumes
    assert first.window_of(first.volumes[0].volume_id) == Interval.of(
        -policy.timing_uncertainty_us,
        10 * MICROS_PER_SECOND + policy.timing_uncertainty_us + 1,
    )
    assert ORIGIN.lat_deg > 0
