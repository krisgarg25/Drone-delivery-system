"""The adversarial geometry from ``technical-build/06-testing-and-safety.md``.

Every scenario is a human description, a machine-readable input, the invariant it
proves, and what it does **not** prove (conventions section 11). The headline case
is the reason the continuous sweep exists: a fast aircraft that passes through
another one between two frames.

Margins come from the repository's policy file, so a scenario's numbers move when
the experiment parameters move; the invariants do not.
"""

from __future__ import annotations

import pytest
from geom4d_scenarios import ORIGIN, PLANE, plan, point_at, waypoint

from droneops_geom_4d import (
    ProtectedBody,
    ProtectedVolumePolicy,
    Verdict,
    VerdictCause,
    conflict_verdict,
    discretised_conflict_verdict,
    frame_conflict_verdict,
    layer_occupancies,
    pair_verdict,
    protected_radius_mm,
    protected_vertical_half_mm,
    static_volume,
    sweep_profile,
)
from droneops_geom_core import MICROS_PER_SECOND, AltitudeReference, LocalPlane

pytestmark = [pytest.mark.unit, pytest.mark.golden]

SECOND = MICROS_PER_SECOND


def reach(policy: ProtectedVolumePolicy, body: ProtectedBody) -> tuple[int, int]:
    """Centre distance at which two protected volumes touch, horizontally and vertically."""
    return (
        2 * protected_radius_mm(body, policy),
        2 * protected_vertical_half_mm(body, policy),
    )


def track(
    source: str,
    *,
    start_us: int,
    duration_s: int,
    before_m: float,
    after_m: float,
    start_altitude_m: float,
    end_altitude_m: float,
    bearing_deg: float = 90.0,
    speed_mmps: int = 200_000,
):
    """A leg that runs through the origin: ``before_m`` behind it, ``after_m`` beyond."""
    return plan(
        [
            waypoint(
                point_at((bearing_deg + 180.0) % 360.0, before_m),
                at_us=start_us,
                altitude_m=start_altitude_m,
                speed_mmps=speed_mmps,
            ),
            waypoint(
                point_at(bearing_deg, after_m),
                at_us=start_us + duration_s * SECOND,
                altitude_m=end_altitude_m,
                speed_mmps=speed_mmps,
            ),
        ],
        plan_version=f"track-{source}",
        source=source,
    )


def test_head_on_on_one_lane_is_a_conflict(policy, body) -> None:
    """Same segment, opposite directions, same ten seconds.

    Invariant: two bodies converging on a shared lane with overlapping time are a
    conflict, and the contact window brackets the meeting instant.
    Does not prove: that the lane is the right place to put a corridor, or that
    ten seconds is a realistic leg. Those are scenario inputs.
    """
    eastbound = track(
        "east",
        start_us=0,
        duration_s=10,
        before_m=100.0,
        after_m=100.0,
        start_altitude_m=40.0,
        end_altitude_m=40.0,
    )
    westbound = track(
        "west",
        start_us=0,
        duration_s=10,
        before_m=100.0,
        after_m=100.0,
        start_altitude_m=40.0,
        end_altitude_m=40.0,
        bearing_deg=270.0,
    )
    result = conflict_verdict(eastbound, westbound, body, policy)
    assert result.verdict is Verdict.CONFLICT
    assert result.cause is VerdictCause.CONTACT
    assert result.windows[0].start <= 5 * SECOND < result.windows[0].end
    assert result.contacts[0].centre_gap_mm <= reach(policy, body)[0]


def test_fast_aircraft_passes_through_another_between_two_frames(policy, body) -> None:
    """The failure mode the sweep exists to prevent.

    Two bodies cross at the origin at 100 m/s, both arriving at the same instant
    which sits exactly between two 1 Hz frames. Checking the frames reports
    clearance; checking the sweep reports contact.

    Invariant: if the continuous sweep finds a conflict, no frame-based check on
    any step coarser than the contact window may be used to clear it.
    Does not prove: that any particular frame rate is safe - the point is that
    none of them is, which is why the verdict here is not a function of a step.
    """
    northbound = track(
        "north",
        start_us=SECOND,
        duration_s=1,
        before_m=100.0,
        after_m=100.0,
        start_altitude_m=40.0,
        end_altitude_m=40.0,
        bearing_deg=0.0,
    )
    eastbound = track(
        "east",
        start_us=SECOND,
        duration_s=1,
        before_m=100.0,
        after_m=100.0,
        start_altitude_m=40.0,
        end_altitude_m=40.0,
    )
    continuous = conflict_verdict(northbound, eastbound, body, policy)
    assert continuous.verdict is Verdict.CONFLICT
    assert continuous.windows[0].start <= 3 * SECOND // 2 < continuous.windows[0].end

    frames = frame_conflict_verdict(northbound, eastbound, body, policy, SECOND)
    assert frames.verdict is Verdict.SAFE
    assert frames.cause is VerdictCause.HORIZONTAL_CLEAR

    conservative = discretised_conflict_verdict(northbound, eastbound, body, policy, SECOND)
    assert conservative.verdict is Verdict.CONFLICT
    # and at every finer step the discretised path keeps reporting the contact
    for step_us in (SECOND, SECOND // 2, 100_000):
        assert (
            discretised_conflict_verdict(northbound, eastbound, body, policy, step_us).verdict
            is Verdict.CONFLICT
        )


def test_a_climb_through_a_layer_is_a_conflict_and_says_which_layer(policy, body, grid) -> None:
    """One body level at 40 m, another climbing through 40 m on the same track.

    Invariant: a climbing body is inside every layer it crosses, with a time
    window per layer widened by the transition buffer.
    Does not prove: that 40 m is a separable altitude. It proves the layer the
    climb passed through is named, with times.
    """
    level = track(
        "level",
        start_us=0,
        duration_s=10,
        before_m=100.0,
        after_m=100.0,
        start_altitude_m=40.0,
        end_altitude_m=40.0,
        speed_mmps=20_000,
    )
    climbing = track(
        "climbing",
        start_us=0,
        duration_s=10,
        before_m=100.0,
        after_m=100.0,
        start_altitude_m=20.0,
        end_altitude_m=60.0,
        speed_mmps=20_000,
    )
    result = conflict_verdict(level, climbing, body, policy)
    assert result.verdict is Verdict.CONFLICT

    occupancies = layer_occupancies(climbing, body, policy, grid.vertical_band_mm)
    assert occupancies
    climbed = [item for item in occupancies if item.manoeuvre.value == "CLIMB"]
    assert climbed
    containing = [
        item for item in occupancies if item.layer.lower_mm <= 40_000 < item.layer.upper_mm
    ]
    assert containing, "the 40 m layer must appear in the climb's occupancy"
    for item in containing:
        assert item.window.end > item.window.start


def test_an_airborne_obstruction_owns_space_and_is_checked_like_a_route(policy, body) -> None:
    """A parked volume at the crossing point: through it is a conflict, over it is not.

    Invariant: a stationary protected volume produces the same typed verdicts as
    a moving one, and vertical clearance above it is a proof, not a guess.
    Does not prove: that the obstruction's height is real - synthetic obstacle
    heights carry their own provenance upstream.
    """
    _horizontal, vertical = reach(policy, body)
    obstruction = static_volume(
        policy=policy,
        body=ProtectedBody.of(1.0, 1.0, "obstruction"),
        plane=PLANE,
        source="world",
        plan_version="obst-1",
        east_mm=0,
        north_mm=0,
        altitude_mm=40_000,
        reference=AltitudeReference.AGL,
        from_us=0,
        to_us=20 * SECOND,
    )
    through = track(
        "through",
        start_us=0,
        duration_s=10,
        before_m=100.0,
        after_m=100.0,
        start_altitude_m=40.0,
        end_altitude_m=40.0,
        speed_mmps=20_000,
    )
    above = plan(
        [
            waypoint(
                PLANE.point_at(
                    LocalPlane(ORIGIN).offset_of(point_at(180.0, 100.0)),
                ),
                at_us=0,
                altitude_m=(40_000 + vertical + 5_000) / 1000.0,
                speed_mmps=20_000,
            ),
            waypoint(
                PLANE.point_at(PLANE.offset_of(point_at(90.0, 100.0))),
                at_us=10 * SECOND,
                altitude_m=(40_000 + vertical + 5_000) / 1000.0,
                speed_mmps=20_000,
            ),
        ],
        plan_version="above",
        source="above",
    )
    assert conflict_verdict(through, above, body, policy).verdict is Verdict.SAFE
    through_prisms = sweep_profile(through, body, policy)
    result = conflict_verdict(
        through,
        track(
            "same-but-lower",
            start_us=0,
            duration_s=10,
            before_m=100.0,
            after_m=100.0,
            start_altitude_m=40.0,
            end_altitude_m=40.0,
            speed_mmps=20_000,
        ),
        body,
        policy,
    )
    assert result.verdict is Verdict.CONFLICT
    assert through_prisms

    meeting = pair_verdict(through_prisms[0], obstruction)
    assert meeting.verdict is Verdict.CONFLICT
    high_prism = sweep_profile(above, body, policy)[0]
    cleared = pair_verdict(high_prism, obstruction)
    assert cleared.verdict is Verdict.SAFE
    assert cleared.cause is VerdictCause.VERTICAL_CLEAR


def test_two_aircraft_identical_except_one_microsecond(policy, body) -> None:
    """A one-microsecond offset is not a separation.

    Invariant: shifting a plan by the smallest representable instant cannot turn a
    conflict into a proof of clearance; the conflict windows stay comparable.
    Does not prove: anything about clock discipline in an adapter - only that the
    kernel's own time unit is not a loophole.
    """
    first = track(
        "first",
        start_us=0,
        duration_s=10,
        before_m=100.0,
        after_m=100.0,
        start_altitude_m=40.0,
        end_altitude_m=40.0,
        speed_mmps=20_000,
    )
    offset = track(
        "offset",
        start_us=1,
        duration_s=10,
        before_m=100.0,
        after_m=100.0,
        start_altitude_m=40.0,
        end_altitude_m=40.0,
        speed_mmps=20_000,
    )
    both = conflict_verdict(first, offset, body, policy)
    assert both.verdict is Verdict.CONFLICT
    assert both.windows[0].measure > 5 * SECOND
    # ... while a whole plan-length apart they are merely neighbours in a lane
    much_later = track(
        "later",
        start_us=100 * SECOND,
        duration_s=10,
        before_m=100.0,
        after_m=100.0,
        start_altitude_m=40.0,
        end_altitude_m=40.0,
        speed_mmps=20_000,
    )
    apart = conflict_verdict(first, much_later, body, policy)
    assert apart.verdict is Verdict.SAFE
    assert apart.cause is VerdictCause.TIME_DISJOINT


def test_same_segment_at_different_times_is_clear(policy, body) -> None:
    """The lane is shared, just not at once: separation through the ledger, not geometry."""
    early = track(
        "early",
        start_us=0,
        duration_s=10,
        before_m=100.0,
        after_m=100.0,
        start_altitude_m=40.0,
        end_altitude_m=40.0,
        speed_mmps=20_000,
    )
    late = track(
        "late",
        start_us=40 * SECOND,
        duration_s=10,
        before_m=100.0,
        after_m=100.0,
        start_altitude_m=40.0,
        end_altitude_m=40.0,
        speed_mmps=20_000,
    )
    result = conflict_verdict(early, late, body, policy)
    assert result.verdict is Verdict.SAFE
    assert result.cause is VerdictCause.TIME_DISJOINT
    # Widening the timing uncertainty until the windows overlap must not create a
    # conflict on its own: the bodies are never in the same place at once.
    padded = ProtectedVolumePolicy(
        schema_version=policy.schema_version,
        policy_version="wide-timing",
        authoritative=False,
        altitude_reference=policy.altitude_reference,
        horizontal_margin_mm=policy.horizontal_margin_mm,
        vertical_margin_mm=policy.vertical_margin_mm,
        position_uncertainty_mm=policy.position_uncertainty_mm,
        timing_uncertainty_us=20 * SECOND,
        transition_buffer_us=policy.transition_buffer_us,
    )
    wider = conflict_verdict(early, late, body, padded)
    assert wider.cause in {VerdictCause.HORIZONTAL_CLEAR, VerdictCause.NEVER_SIMULTANEOUS}
    assert wider.verdict is not Verdict.CONFLICT


def test_horizontal_and_vertical_uncertainty_are_separate_inputs(policy, body) -> None:
    """Retuning one axis cannot silently change the other.

    Invariant: a pair cleared by vertical separation stays cleared when the
    horizontal margin is tripled, and stops being cleared only when the *vertical*
    margin grows enough to close the gap.
    Does not prove: that either margin is adequate - only that the four policy
    inputs stay four inputs.
    """
    horizontal, vertical = reach(policy, body)
    separation_m = (3 * vertical // 2) / 1000.0
    low = track(
        "low",
        start_us=0,
        duration_s=10,
        before_m=100.0,
        after_m=100.0,
        start_altitude_m=40.0,
        end_altitude_m=40.0,
        speed_mmps=20_000,
    )
    high = track(
        "high",
        start_us=0,
        duration_s=10,
        before_m=100.0,
        after_m=100.0,
        start_altitude_m=40.0 + separation_m,
        end_altitude_m=40.0 + separation_m,
        speed_mmps=20_000,
    )
    baseline = conflict_verdict(low, high, body, policy)
    assert baseline.verdict is Verdict.SAFE
    assert baseline.cause is VerdictCause.VERTICAL_CLEAR

    wider_horizontal = ProtectedVolumePolicy(
        schema_version=policy.schema_version,
        policy_version="wider-horizontal",
        authoritative=False,
        altitude_reference=policy.altitude_reference,
        horizontal_margin_mm=policy.horizontal_margin_mm * 3,
        vertical_margin_mm=policy.vertical_margin_mm,
        position_uncertainty_mm=policy.position_uncertainty_mm * 3,
        timing_uncertainty_us=policy.timing_uncertainty_us,
        transition_buffer_us=policy.transition_buffer_us,
    )
    assert conflict_verdict(low, high, body, wider_horizontal).cause is VerdictCause.VERTICAL_CLEAR

    taller_vertical = ProtectedVolumePolicy(
        schema_version=policy.schema_version,
        policy_version="taller-vertical",
        authoritative=False,
        altitude_reference=policy.altitude_reference,
        horizontal_margin_mm=policy.horizontal_margin_mm,
        vertical_margin_mm=policy.vertical_margin_mm * 3,
        position_uncertainty_mm=policy.position_uncertainty_mm,
        timing_uncertainty_us=policy.timing_uncertainty_us,
        transition_buffer_us=policy.transition_buffer_us,
    )
    closed = conflict_verdict(low, high, body, taller_vertical)
    assert closed.verdict is Verdict.CONFLICT
    assert horizontal > 0
