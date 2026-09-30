"""Hypothesis property tests: the invariants the swept-volume kernel cannot break.

Run derandomised, so a failure reproduces exactly and a replay of the same suite
hashes the same way (conventions section 4). The generators produce adversarial
shapes: zero-length chords, coincident legs, reversed headings, tiny steps,
altitude inversions and legs that start at the same microsecond.

Invariants proved here, and what each does *not* prove:

1. **Containment** - a discretised sweep covers the continuous sweep at every
   step. Does not prove the step is cheap enough to use.
2. **Symmetry** - ``pair_verdict(a, b)`` and ``pair_verdict(b, a)`` agree. Does not
   prove the answer is right, only that it does not depend on argument order.
3. **Vertical clearance** - separated by more than the sum of the vertical
   reaches, on identical tracks, is ``SAFE`` with ``VERTICAL_CLEAR``. Does not
   prove that separation is operationally sufficient.
4. **Timing monotonicity** - widening timing uncertainty can only add conflict
   windows, never remove them. Does not prove the policy's value is correct.
5. **Pre-filter coverage** - the cell/time index never discards a pair the exact
   sweep would flag. Does not prove the index is selective.
"""

from __future__ import annotations

import math
from dataclasses import replace

from geom4d_scenarios import PLANE, plan, waypoint
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from droneops_geom_4d import (
    IndexedVolume,
    ProtectedBody,
    ProtectedVolumePolicy,
    RouteProfile,
    SweepBasis,
    Verdict,
    VerdictCause,
    discretised_sweep,
    index_volumes,
    pair_verdict,
    profile_verdict,
    protected_radius_mm,
    protected_vertical_half_mm,
    sweep_profile,
)
from droneops_geom_core import (
    CHANDIGARH_UTM_EPSG,
    MICROS_PER_SECOND,
    Altitude,
    GeoPoint,
    GridSpec,
    Millimetre,
    PlanarOffset,
    as_millimetres,
)

pytestmark = __import__("pytest").mark.property

SECOND = MICROS_PER_SECOND

# Kept small and derandomised: the point is the invariant, not coverage theatre.
PROFILE_SETTINGS = settings(
    max_examples=25,
    deadline=None,
    derandomize=True,
    suppress_health_check=[HealthCheck.too_slow],
)

STEP_US = st.sampled_from([5 * SECOND, SECOND, 200_000, 37_000])


def point_from(east_mm: int, north_mm: int, altitude_mm: int) -> tuple[GeoPoint, int]:
    """A geodetic point at a projected offset, on the shared plane."""
    return PLANE.point_at(
        PlanarOffset(as_millimetres(east_mm), as_millimetres(north_mm))
    ), altitude_mm


@st.composite
def track_profile(draw: st.DrawFn) -> RouteProfile:
    """A one- or two-leg profile over a coarse millimetre lattice."""
    legs = draw(st.integers(1, 2))
    east = draw(st.integers(-40, 40)) * 1_000
    north = draw(st.integers(-40, 40)) * 1_000
    altitude = draw(st.integers(0, 80)) * 1_000
    at_us = draw(st.integers(0, 4)) * SECOND
    points = []
    position, height = point_from(east, north, altitude)
    points.append(waypoint(position, at_us=at_us, altitude_m=height / 1000.0, speed_mmps=200_000))
    for _ in range(legs):
        east += draw(st.integers(-40, 40)) * 1_000
        north += draw(st.integers(-40, 40)) * 1_000
        altitude += draw(st.integers(-20, 30)) * 1_000
        at_us += draw(st.integers(1, 6)) * SECOND
        position, height = point_from(east, north, max(altitude, 0))
        points.append(
            waypoint(position, at_us=at_us, altitude_m=height / 1000.0, speed_mmps=200_000)
        )
    return plan(points, plan_version="generated", source="generated")


def shifted_copy(profile: RouteProfile, altitude_delta_mm: int) -> RouteProfile:
    """The same law, flown ``altitude_delta_mm`` higher, on the same plane."""
    points = [
        waypoint(
            point.position,
            at_us=point.at_us,
            altitude_m=point.altitude.value_mm / 1000.0,
            speed_mmps=point.speed_mmps,
        )
        for point in profile.points
    ]
    lifted = [
        replace(
            point,
            altitude=Altitude(
                point.altitude.reference, Millimetre(point.altitude.value_mm + altitude_delta_mm)
            ),
        )
        for point in points
    ]
    return plan(
        lifted, plan_version=profile.plan_version + "-lifted", source=profile.source + "-lifted"
    )


@PROFILE_SETTINGS
@given(profile=track_profile(), step_us=STEP_US)
def test_discretised_sweep_always_contains_the_continuous_sweep(
    profile: RouteProfile, step_us: int, policy: ProtectedVolumePolicy, body: ProtectedBody
) -> None:
    """Invariant 1: containment holds for every generated profile and every step."""
    continuous = sweep_profile(profile, body, policy)
    samples = discretised_sweep(profile, body, policy, step_us)
    assert samples
    for prism in continuous:
        assert prism.basis is SweepBasis.CONTINUOUS
        for at_us in (
            prism.motion_start_us,
            prism.motion_end_us,
            (prism.motion_start_us + prism.motion_end_us) // 2,
        ):
            wanted = prism.instantaneous_slice(at_us)
            covering = [item for item in samples if item.window().contains_instant(at_us)]
            assert covering, f"{at_us} is owned by no sample at step {step_us}"
            assert any(
                item.radius_mm >= wanted.radius_mm
                and item.vertical_half_mm >= wanted.vertical_half_mm
                and item.instantaneous_slice(at_us).contains(
                    wanted.east_mm, wanted.north_mm, wanted.altitude_mm
                )
                for item in covering
            )


@PROFILE_SETTINGS
@given(left=track_profile(), right=track_profile())
def test_intersection_is_symmetric(
    left: RouteProfile, right: RouteProfile, policy: ProtectedVolumePolicy, body: ProtectedBody
) -> None:
    """Invariant 2: the verdict does not depend on which aircraft was asked about first."""
    a = sweep_profile(left, body, policy)
    b = sweep_profile(right, body, policy)
    for first in a:
        for second in b:
            forward = pair_verdict(first, second)
            backward = pair_verdict(second, first)
            assert forward.verdict is backward.verdict
            assert forward.windows == backward.windows
    assert profile_verdict(a, b).verdict is profile_verdict(b, a).verdict


@PROFILE_SETTINGS
@given(profile=track_profile())
def test_no_conflict_when_vertical_separation_exceeds_the_sum_of_margins(
    profile: RouteProfile, policy: ProtectedVolumePolicy, body: ProtectedBody
) -> None:
    """Invariant 3: separated by more than two vertical reaches, always with the same law.

    The clearance is one millimetre beyond the exact bound, so this is a test of
    the arithmetic, not of a comfortable margin. Temporal padding is switched off
    deliberately: during a pad one body has parked at its leg end while the other
    is still climbing, so a shifted copy is not vertically clear there - which is
    exactly what the timing-uncertainty input is for.
    """
    unpadded = replace(policy, timing_uncertainty_us=0, transition_buffer_us=0)
    separation = 2 * protected_vertical_half_mm(body, policy) + 1
    other = shifted_copy(profile, separation)
    result = profile_verdict(
        sweep_profile(profile, body, unpadded), sweep_profile(other, body, unpadded)
    )
    assert result.verdict is Verdict.SAFE
    assert result.cause is VerdictCause.VERTICAL_CLEAR
    assert result.windows == ()
    # one millimetre inside the bound and the same pair is no longer clear
    touching = shifted_copy(profile, separation - 2)
    assert (
        profile_verdict(
            sweep_profile(profile, body, unpadded), sweep_profile(touching, body, unpadded)
        ).verdict
        is Verdict.CONFLICT
    )


@PROFILE_SETTINGS
@given(
    left=track_profile(),
    right=track_profile(),
    extra_us=st.sampled_from([1_000_000 * n for n in range(1, 31)]),
)
def test_timing_uncertainty_is_monotonic_in_conflict_windows(
    left: RouteProfile,
    right: RouteProfile,
    extra_us: int,
    policy: ProtectedVolumePolicy,
    body: ProtectedBody,
) -> None:
    """Invariant 4: more timing uncertainty can only ever add contested time."""
    tight = replace(policy, timing_uncertainty_us=0)
    wider = replace(policy, timing_uncertainty_us=policy.timing_uncertainty_us + extra_us)
    narrow = profile_verdict(sweep_profile(left, body, tight), sweep_profile(right, body, tight))
    broad = profile_verdict(sweep_profile(left, body, wider), sweep_profile(right, body, wider))
    for window in narrow.windows:
        leftovers = window.subtract(broad.windows)
        assert leftovers == (), f"{window} lost time when uncertainty grew"
    if narrow.verdict is Verdict.CONFLICT:
        assert broad.verdict is Verdict.CONFLICT


@PROFILE_SETTINGS
@given(left=track_profile(), right=track_profile())
def test_the_prefilter_never_discards_a_real_conflict(
    left: RouteProfile,
    right: RouteProfile,
    grid,
    policy: ProtectedVolumePolicy,
    body: ProtectedBody,
) -> None:
    """Invariant 5: cell-and-window candidates are a superset of exact contacts."""

    lattice = GridSpec(
        epsg=CHANDIGARH_UTM_EPSG,
        cell_size_mm=as_millimetres(10_000),
        vertical_band_mm=as_millimetres(5_000),
    )
    a = sweep_profile(left, body, policy)
    b = sweep_profile(right, body, policy)
    left_index = index_volumes(a, lattice, level=1, max_cells=20_000)
    right_index = index_volumes(b, lattice, level=1, max_cells=20_000)
    candidates = set(left_index.candidates(right_index))
    for first in a:
        for second in b:
            if pair_verdict(first, second).verdict is Verdict.CONFLICT:
                assert (first.volume_id, second.volume_id) in candidates
    assert isinstance(left_index.volumes[0], IndexedVolume)
    assert math.isfinite(float(protected_radius_mm(body, policy)))
