"""The three sweep representations, and what each one is allowed to claim.

Continuous prisms carry a motion law; discretised prisms are stationary but
inflated so their union *contains* the continuous sweep for every step size;
frame snapshots are discs at instants with nothing in between, which is exactly
the representation that lets a fast aircraft pass through another one unseen.

What these do not prove: that a step size used here is the right step for an
operation. Containment is shown for several steps; the property suite shows it for
every step a generator can produce.
"""

from __future__ import annotations

import pytest
from geom4d_scenarios import PLANE, straight_profile, two_leg_profile

from droneops_geom_4d import (
    ProfileError,
    ProtectedBody,
    ProtectedVolumePolicy,
    SweepBasis,
    SweepGeometryError,
    conflict_verdict,
    discretised_sweep,
    frame_snapshots,
    protected_radius_mm,
    protected_vertical_half_mm,
    shapes,
    static_volume,
    sweep_profile,
    volumes_in_order,
)
from droneops_geom_core import MICROS_PER_SECOND, AltitudeReference, Interval

pytestmark = pytest.mark.unit


def test_one_prism_per_leg_in_leg_order(policy, body: ProtectedBody) -> None:
    profile = two_leg_profile(
        first_m=60.0,
        second_m=60.0,
        bearing_deg=90.0,
        turn_bearing_deg=0.0,
        start_altitude_m=30.0,
        end_altitude_m=60.0,
    )
    prisms = sweep_profile(profile, body, policy)
    assert len(prisms) == 2
    assert [prism.segment_index for prism in prisms] == [0, 1]
    assert prisms[0].volume_id != prisms[1].volume_id
    assert all(prism.basis is SweepBasis.CONTINUOUS for prism in prisms)


def test_the_protected_extent_is_body_plus_the_two_policy_inputs_it_owns(
    policy, body: ProtectedBody
) -> None:
    """Horizontal: body + margin + navigation uncertainty. Vertical: body + margin."""
    assert protected_radius_mm(body, policy) == 150 + 15_000 + 5_000
    assert protected_vertical_half_mm(body, policy) == 100 + 10_000
    prism = sweep_profile(straight_profile(distance_m=100.0, bearing_deg=90.0), body, policy)[0]
    assert prism.radius_mm == protected_radius_mm(body, policy)
    assert prism.vertical_half_mm == protected_vertical_half_mm(body, policy)
    assert prism.policy_key == policy.key()


def test_a_climb_leg_carries_the_transition_buffer_a_level_leg_does_not(
    policy, body: ProtectedBody
) -> None:
    profile = two_leg_profile(
        first_m=60.0,
        second_m=60.0,
        bearing_deg=90.0,
        turn_bearing_deg=90.0,
        start_altitude_m=30.0,
        end_altitude_m=60.0,
        duration_s=10,
    )
    level, climbing = sweep_profile(profile, body, policy)
    assert level.pad_us == policy.timing_uncertainty_us
    assert climbing.pad_us == policy.timing_uncertainty_us + policy.transition_buffer_us


def test_the_continuous_sweep_knows_where_it_was_at_an_unsampled_instant(
    policy, body: ProtectedBody
) -> None:
    profile = straight_profile(distance_m=100.0, bearing_deg=90.0, duration_s=10)
    prism = sweep_profile(profile, body, policy)[0]
    slice_at_1s = prism.instantaneous_slice(1 * MICROS_PER_SECOND)
    assert abs(slice_at_1s.east_mm - 10_000) <= 2
    assert abs(slice_at_1s.north_mm) <= 2
    assert slice_at_1s.altitude_mm == 40_000
    # 1 mm off the track, 9.9 m along it: protected airspace
    assert slice_at_1s.contains(9_901, 0, 40_000)
    # 25 m off the track: outside the protected volume, by the policy's own arithmetic
    assert not slice_at_1s.contains(34_000, 0, 40_000)
    assert prism.contains(9_901, 0, 40_000, 1 * MICROS_PER_SECOND)
    assert slice_at_1s.band.lower_mm <= slice_at_1s.altitude_mm < slice_at_1s.band.upper_mm


def test_window_and_band_are_conservative_over_the_whole_leg(policy, body: ProtectedBody) -> None:
    profile = two_leg_profile(
        first_m=40.0,
        second_m=40.0,
        bearing_deg=90.0,
        turn_bearing_deg=90.0,
        start_altitude_m=30.0,
        end_altitude_m=60.0,
        duration_s=10,
    )
    climbing = sweep_profile(profile, body, policy)[1]
    window = climbing.window()
    assert window.start == 10 * MICROS_PER_SECOND - climbing.pad_us
    assert window.end == 20 * MICROS_PER_SECOND + climbing.pad_us + 1
    band = climbing.band()
    vertical = climbing.vertical_half_mm
    assert band.lower_mm == 30_000 - vertical
    assert band.upper_mm == 60_000 + vertical + 1


def test_the_footprint_is_the_capsule_the_body_actually_sweeps(policy, body: ProtectedBody) -> None:
    """A buffered straight leg *is* the union of its moving discs: exact, not approximated."""
    prism = sweep_profile(straight_profile(distance_m=100.0, bearing_deg=90.0), body, policy)[0]
    footprint = prism.footprint_wkt()
    assert shapes.is_usable(footprint)
    assert shapes.contains_xy(footprint, 0, 0)
    assert shapes.contains_xy(footprint, 50_000, 0)
    assert shapes.contains_xy(footprint, 100_000, 0)
    assert not shapes.contains_xy(footprint, 50_000, prism.radius_mm + 10)


@pytest.mark.parametrize("step_us", [20_000_000, 5_000_000, 1_000_000, 250_000, 37_000])
def test_discretised_union_contains_the_continuous_sweep_at_every_step(
    policy, body: ProtectedBody, step_us: int
) -> None:
    """The reconciliation property, in one concrete case.

    Whatever the step, some discretised prism owns each instant, and its slice
    contains the continuous slice's own centre with a reach at least as large. If
    this ever failed, the discretised path could hand a checker a conflict-free
    schedule the continuous path disagrees with.
    """
    profile = two_leg_profile(
        first_m=100.0,
        second_m=100.0,
        bearing_deg=90.0,
        turn_bearing_deg=45.0,
        start_altitude_m=30.0,
        end_altitude_m=70.0,
        duration_s=20,
        speed_mmps=5_000,
    )
    continuous = sweep_profile(profile, body, policy)
    samples = discretised_sweep(profile, body, policy, step_us)
    assert samples
    for prism in continuous:
        for at_us in (
            prism.motion_start_us,
            prism.motion_end_us,
            (prism.motion_start_us + prism.motion_end_us) // 2,
        ):
            wanted = prism.instantaneous_slice(at_us)
            covering = [sample for sample in samples if sample.window().contains_instant(at_us)]
            assert covering, f"{at_us} us is owned by no sample at step {step_us}"
            assert any(
                sample.radius_mm >= wanted.radius_mm
                and sample.vertical_half_mm >= wanted.vertical_half_mm
                and sample.instantaneous_slice(at_us).contains(
                    wanted.east_mm, wanted.north_mm, wanted.altitude_mm
                )
                for sample in covering
            ), f"nothing covers {wanted} at {at_us} us"


def test_frame_snapshots_leave_gaps_a_fast_body_can_walk_through(
    policy, body: ProtectedBody
) -> None:
    """The failure mode, stated as a fact about the representation."""
    profile = straight_profile(distance_m=100.0, bearing_deg=90.0, duration_s=10, speed_mmps=10_000)
    frames = frame_snapshots(profile, body, policy, 5 * MICROS_PER_SECOND)
    assert [frame.motion_start_us for frame in frames] == [
        0,
        5 * MICROS_PER_SECOND,
        10 * MICROS_PER_SECOND,
    ]
    assert all(frame.pad_us == 0 for frame in frames)
    assert all(frame.basis is SweepBasis.FRAME_SAMPLE for frame in frames)
    # an instant between two frames is inside no frame window at all
    assert not any(frame.window().contains_instant(2 * MICROS_PER_SECOND) for frame in frames)
    # ... while both the continuous and the conservative sweep own that instant
    assert any(
        prism.window().contains_instant(2 * MICROS_PER_SECOND)
        for prism in sweep_profile(profile, body, policy)
    )
    assert any(
        prism.window().contains_instant(2 * MICROS_PER_SECOND)
        for prism in discretised_sweep(profile, body, policy, 5 * MICROS_PER_SECOND)
    )


def test_a_bad_step_is_a_rejection(policy, body: ProtectedBody) -> None:
    profile = straight_profile(distance_m=10.0, bearing_deg=90.0)
    for builder in (discretised_sweep, frame_snapshots):
        with pytest.raises(SweepGeometryError, match="must be positive"):
            builder(profile, body, policy, 0)


def test_a_policy_in_another_frame_refuses_the_plan(policy, body: ProtectedBody) -> None:
    """Converting AGL to AMSL needs site datums; guessing them is not this package's job."""
    other = ProtectedVolumePolicy(
        schema_version=policy.schema_version,
        policy_version="other-frame",
        authoritative=False,
        altitude_reference=AltitudeReference.AMSL,
        horizontal_margin_mm=policy.horizontal_margin_mm,
        vertical_margin_mm=policy.vertical_margin_mm,
        position_uncertainty_mm=policy.position_uncertainty_mm,
        timing_uncertainty_us=policy.timing_uncertainty_us,
        transition_buffer_us=policy.transition_buffer_us,
    )
    profile = straight_profile(distance_m=100.0, bearing_deg=90.0)
    with pytest.raises(SweepGeometryError, match="AGL"):
        sweep_profile(profile, body, other)
    with pytest.raises(SweepGeometryError, match="AMSL"):
        static_volume(
            policy=other,
            body=body,
            plane=PLANE,
            source="world",
            plan_version="obst",
            east_mm=0,
            north_mm=0,
            altitude_mm=40_000,
            reference=AltitudeReference.AGL,
            from_us=0,
            to_us=1,
        )


def test_a_stationary_obstruction_is_a_volume_like_any_other(policy, body: ProtectedBody) -> None:
    """An airborne obstruction occupies space over time, so it shares the code path."""
    obstruction = static_volume(
        policy=policy,
        body=ProtectedBody.of(1.0, 1.0, "obstruction"),
        plane=PLANE,
        source="world",
        plan_version="obst-1",
        east_mm=50_000,
        north_mm=0,
        altitude_mm=40_000,
        reference=AltitudeReference.AGL,
        from_us=0,
        to_us=30 * MICROS_PER_SECOND,
    )
    assert obstruction.basis is SweepBasis.CONTINUOUS
    assert obstruction.is_moving() is False
    assert obstruction.plane_origin == PLANE.reference.sort_key()
    assert obstruction.window() == Interval.of(
        -policy.timing_uncertainty_us,
        30 * MICROS_PER_SECOND + policy.timing_uncertainty_us + 1,
    )
    assert obstruction.instantaneous_slice(15 * MICROS_PER_SECOND).contains(50_900, 0, 40_000)
    assert not obstruction.instantaneous_slice(15 * MICROS_PER_SECOND).contains(90_000, 0, 40_000)


def test_sweep_order_is_explicit_and_deterministic(policy, body: ProtectedBody) -> None:
    profile = two_leg_profile(
        first_m=50.0,
        second_m=50.0,
        bearing_deg=90.0,
        turn_bearing_deg=180.0,
        start_altitude_m=40.0,
        end_altitude_m=40.0,
    )
    prisms = sweep_profile(profile, body, policy)
    assert volumes_in_order(list(reversed(prisms))) == volumes_in_order(prisms)
    # A plan always conflicts with itself: the kernel is not vacuously safe.
    assert conflict_verdict(profile, profile, body, policy).verdict.value == "CONFLICT"


def test_a_profile_that_cannot_move_is_refused_before_it_is_swept(
    policy, body: ProtectedBody
) -> None:
    with pytest.raises(ProfileError):
        straight_profile(distance_m=10.0, bearing_deg=90.0, duration_s=0)
    assert sweep_profile(straight_profile(distance_m=10.0, bearing_deg=90.0), body, policy)
