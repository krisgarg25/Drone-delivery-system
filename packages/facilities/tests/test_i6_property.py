"""Invariant I6 over arbitrary input: ``max(overlap_count(commitments)) <= capacity``.

This is the property test named in ``technical-build/06`` section 3, run against
the real surface rather than a purpose-built mock: :class:`PadPool` for the pure
pad statement, :class:`FacilityScheduler` for the full chain. Adversarial values
are in scope - empty intervals, intervals before the epoch, identical arrivals,
clock-jump spans and a capacity of zero.

The sweep-line peak is also checked against a brute-force point scan, so the
measurement of the invariant does not rest on the same code the invariant is
about.

What this does not prove: that work is fairly distributed, that waits are short,
or that first-fit finds the best placement. It bounds overbooking and shows that
a refusal is caused by obstruction, not by a silent drop.
"""

from __future__ import annotations

import pytest
from facilities_support import SECOND_US, make_profile, make_request
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from droneops_facilities import (
    CapacityProfile,
    FacilityScheduler,
    PadPool,
    claim_windows,
    commit_many,
    load_capacity_profiles,
    overlap_count,
)
from droneops_geom_core import Interval

pytestmark = pytest.mark.property

PROFILE_SETTINGS = settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
SMALL = 40 * SECOND_US


@st.composite
def time_window(draw: st.DrawFn, *, floor: int = -20, span: int = 120) -> Interval:
    """An arrival window: whole seconds, sometimes empty, sometimes before zero."""
    start = draw(st.integers(min_value=floor, max_value=span))
    length = draw(st.integers(min_value=0, max_value=45))
    return Interval.of(start * SECOND_US, (start + length) * SECOND_US)


@st.composite
def arrival_batch(draw: st.DrawFn) -> tuple[int, tuple[Interval, ...]]:
    capacity = draw(st.integers(min_value=0, max_value=5))
    windows = draw(st.lists(time_window(), min_size=2, max_size=9))
    return capacity, tuple(windows)


def _brute_force_peak(intervals: tuple[Interval, ...]) -> int:
    """Count coverage at every endpoint and mid-instant: an independent peak."""
    live = [interval for interval in intervals if not interval.is_empty]
    if not live:
        return 0
    probes: set[int] = set()
    for interval in live:
        probes.update((interval.start, interval.end - 1, (interval.start + interval.end) // 2))
    return max(sum(1 for held in live if held.contains_instant(probe)) for probe in probes)


# --- the spec's statement, literally ---------------------------------------


@given(capacity=st.integers(1, 5), arrival_windows=st.lists(time_window(), min_size=2))
@PROFILE_SETTINGS
def test_pad_capacity_never_exceeded(capacity: int, arrival_windows: list[Interval]) -> None:
    commitments = commit_many(arrival_windows, capacity)
    assert max([overlap_count(claim_windows(commitments))] or [0]) <= capacity


@given(data=arrival_batch())
@PROFILE_SETTINGS
def test_peak_overlap_never_exceeds_capacity(data: tuple[int, tuple[Interval, ...]]) -> None:
    capacity, windows = data
    outcome = PadPool("hub-prop", capacity).commit_many(windows)
    peak = outcome.peak_overlap
    assert peak <= capacity
    assert outcome.holds_i6
    assert peak == _brute_force_peak(claim_windows(outcome.commitments))


@given(data=arrival_batch())
@PROFILE_SETTINGS
def test_no_single_pad_is_ever_double_booked(data: tuple[int, tuple[Interval, ...]]) -> None:
    capacity, windows = data
    pool = PadPool("hub-unit", capacity)
    outcome = pool.commit_many(windows)
    for unit in pool.units:
        held = pool.held_windows(unit.identity_token())
        assert overlap_count(held) <= 1, unit.identity_token()
    assert len(outcome.commitments) == sum(
        len(pool.held_windows(unit.identity_token())) for unit in pool.units
    )


@given(data=arrival_batch())
@PROFILE_SETTINGS
def test_a_refusal_is_obstruction_never_a_silent_drop(data: tuple[int, tuple[Interval, ...]]) -> None:
    """Queueing, not vanishing: every refused non-empty window is blocked per pad."""
    capacity, windows = data
    pool = PadPool("hub-cause", capacity)
    outcome = pool.commit_many(windows)
    identities = {unit.identity_token() for unit in pool.units}
    for rejected in outcome.rejected:
        assert rejected.reason.value == "pad_wait"
        if rejected.window.is_empty:
            # An empty interval reserves nothing, so it is refused as a claim,
            # not queued as work: that is a data error, not overbooking.
            assert not rejected.obstructed
            continue
        assert {identity for identity, _ in rejected.blocked_by} == identities
        for identity, held in rejected.blocked_by:
            assert identity in identities
            assert held.overlaps(rejected.window)


@given(capacity=st.integers(1, 4), windows=st.lists(time_window(), min_size=1, max_size=6))
@PROFILE_SETTINGS
def test_admitting_more_of_a_batch_never_raises_the_capacity_line(
    capacity: int, windows: list[Interval]
) -> None:
    """Order changes who waits, never whether the site is overbooked."""
    forward = PadPool("hub-order", capacity).commit_many(windows)
    reverse = PadPool("hub-order", capacity).commit_many(tuple(reversed(windows)))
    assert forward.peak_overlap <= capacity
    assert reverse.peak_overlap <= capacity
    # The accepted count is conserved across the two orders only when capacity is
    # not the binding constraint; the line itself must hold in both.
    assert max(forward.peak_overlap, reverse.peak_overlap) <= capacity


# --- the whole scheduler ----------------------------------------------------


@st.composite
def facility_and_arrivals(draw: st.DrawFn) -> tuple[CapacityProfile, tuple[object, ...]]:
    pads = draw(st.integers(min_value=1, max_value=3))
    chargers = draw(st.integers(min_value=0, max_value=2))
    crew = draw(st.integers(min_value=0, max_value=2))
    closes = draw(st.integers(min_value=6, max_value=60))
    profile = make_profile(
        "hub-prop-full",
        pads=pads,
        charger_bays=chargers,
        crew_slots=crew,
        opens_min=0,
        closes_min=closes,
        crew_shifts=((0, closes),),
    )
    count = draw(st.integers(min_value=2, max_value=6))
    requests = tuple(
        make_request(
            f"req-{index}",
            "hub-prop-full",
            ready_s=draw(st.integers(min_value=0, max_value=closes * 60 - 240)),
            deadline_s=closes * 60,
            recharge_us=draw(st.sampled_from([0, 30 * SECOND_US, 3 * SMALL])),
            allow_queue=True,
        )
        for index in range(count)
    )
    return profile, requests


@given(data=facility_and_arrivals())
@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_the_full_chain_never_exceeds_any_declared_capacity(
    data: tuple[CapacityProfile, tuple[object, ...]],
) -> None:
    profile, requests = data
    scheduler = FacilityScheduler([profile])
    outcome = scheduler.schedule(requests)  # type: ignore[arg-type]
    audit = scheduler.audit(profile.facility_id)
    assert audit.i6_holds, audit.as_metrics()
    assert audit.pads.peak_overlap <= profile.pad_count
    assert audit.chargers.peak_overlap <= profile.charger_bays
    assert audit.crew.peak_overlap <= profile.crew_slots
    # Nothing disappeared: every arrival is scheduled, waiting or refused.
    assert len(outcome.scheduled) + len(outcome.queued) + len(outcome.rejected) == len(requests)
    assert all(not claim.window.is_empty for plan in outcome.scheduled for claim in plan.claims)


def test_the_shipped_bundle_schedules_without_overbooking() -> None:
    """Every pad, bay and crew slot in the real synthetic bundle stays inside I6."""
    from facilities_support import load_bundle_document

    profiles = load_capacity_profiles(load_bundle_document())
    requests = tuple(
        make_request(
            f"req-{site.facility_id}-{index}",
            site.facility_id,
            ready_s=index * 90,
            deadline_s=60 * 60,
            recharge_us=2 * 60 * SECOND_US,
        )
        for site in profiles
        for index in range(4)
    )
    scheduler = FacilityScheduler(profiles)
    scheduler.schedule(requests)
    assert all(audit.i6_holds for audit in scheduler.audit_all())
