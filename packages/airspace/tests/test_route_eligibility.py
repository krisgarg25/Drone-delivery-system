"""Route eligibility: geometry, band, window and condition, decided separately.

A route is blocked only when all four axes meet: the footprint is crossed, the vertical
bands overlap, the restriction's window overlaps the occupancy, and the data is trustworthy.
Each axis gets its own test so a change that silently drops one is caught by exactly one
failing assertion rather than by a mysterious aggregate.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from airspace_support import (
    CLEAR_ROUTE,
    NOW,
    buffered_footprint,
    circle_footprint,
    policy,
    restriction,
    route,
    snapshot,
    untrusted_restriction,
)

from droneops_airspace import (
    AirspaceError,
    CorridorEdgeProfile,
    EligibilityVerdict,
    NotAFlightAuthorisationError,
    RestrictionKind,
    RestrictionSnapshot,
    RouteWaypoint,
    evaluate_corridor_edges,
    evaluate_route,
)
from droneops_contracts.geometry import AltitudeReference

pytestmark = pytest.mark.unit


def test_crossing_a_live_red_zone_is_blocked_now() -> None:
    result = evaluate_route(route(), snapshot(restriction()), now=NOW, policy=policy())
    assert result.verdict is EligibilityVerdict.BLOCKED_NOW
    assert result.launch_permitted is False
    assert {report.subject for report in result.reports} == {
        "segment 0 (0s->300s)",
        "segment 1 (300s->600s)",
    }


def test_route_that_avoids_the_zone_is_allowed_by_the_model_only() -> None:
    result = evaluate_route(route(CLEAR_ROUTE), snapshot(restriction()), now=NOW, policy=policy())
    assert result.verdict is EligibilityVerdict.ALLOWED_BY_MODEL
    assert result.launch_permitted is True
    assert result.reports == ()
    assert "Simulation model only" in result.note


def test_flying_above_the_band_is_not_blocked() -> None:
    high = route(altitude_m=199.0)
    result = evaluate_route(high, snapshot(restriction(ceiling_m=120.0)), now=NOW, policy=policy())
    assert result.verdict is EligibilityVerdict.ALLOWED_BY_MODEL


def test_an_open_band_catches_any_plausible_height() -> None:
    near_airport = route(((30.68, 76.79), (30.69, 76.79)), altitude_m=400.0)
    result = evaluate_route(
        near_airport,
        snapshot(
            restriction(
                kind=RestrictionKind.AIRPORT_VICINITY,
                footprint=circle_footprint(),
                ceiling_m=None,
            )
        ),
        now=NOW,
        policy=policy(),
    )
    assert result.verdict is EligibilityVerdict.ALLOWED_WITH_CONDITION
    assert result.outstanding_conditions == ("airport_coordination_recorded",)


def test_a_closed_and_fresh_window_no_longer_applies() -> None:
    ended = restriction(effective_until=NOW - timedelta(minutes=30))
    result = evaluate_route(route(), snapshot(ended), now=NOW, policy=policy())
    assert result.verdict is EligibilityVerdict.ALLOWED_BY_MODEL
    assert result.launch_permitted is True


def test_a_closure_that_starts_mid_route_is_blocked_during_the_window() -> None:
    """The distinction the planner needs: reroute versus reschedule."""

    upcoming = restriction(effective_from=NOW + timedelta(seconds=400))
    result = evaluate_route(route(), snapshot(upcoming), now=NOW, policy=policy())
    assert result.verdict is EligibilityVerdict.BLOCKED_DURING_WINDOW
    assert result.launch_permitted is False
    late = [report for report in result.reports if report.verdict.value == "blocked_during_window"]
    assert late, result.reports
    assert late[0].blocked_from == NOW + timedelta(seconds=400)
    assert late[0].blocked_until == NOW + timedelta(seconds=600)
    assert any("reschedule" in reason for reason in late[0].reasons)


def test_stale_data_on_the_crossed_zone_is_a_no_launch() -> None:
    result = evaluate_route(route(), snapshot(untrusted_restriction()), now=NOW, policy=policy())
    assert result.verdict is EligibilityVerdict.NO_LAUNCH_INSUFFICIENT_DATA
    assert result.launch_permitted is False
    assert all(
        report.trust in {"stale", "expired_provenance", "unusable"} for report in result.reports
    )


def test_stale_data_500_km_away_does_not_stop_this_route() -> None:
    """Fail closed where the claim could bite, not everywhere the file mentions."""

    elsewhere = untrusted_restriction(
        footprint=buffered_footprint(),
    )
    result = evaluate_route(route(CLEAR_ROUTE), snapshot(elsewhere), now=NOW, policy=policy())
    assert result.verdict is EligibilityVerdict.ALLOWED_BY_MODEL


def test_a_snapshot_with_no_source_behind_it_cannot_clear_anything() -> None:
    result = evaluate_route(
        route(CLEAR_ROUTE), RestrictionSnapshot.no_feed(NOW), now=NOW, policy=policy()
    )
    assert result.verdict is EligibilityVerdict.NO_LAUNCH_INSUFFICIENT_DATA
    assert result.snapshot is not None
    assert result.snapshot.data_usable is False
    assert "ignorance, not clear airspace" in result.snapshot.reasons[0]


def test_a_stale_snapshot_fails_closed_even_when_it_lists_nothing() -> None:
    empty = snapshot()
    stale = empty.model_copy(update={"fetched_at": NOW - timedelta(days=3)})
    result = evaluate_route(route(CLEAR_ROUTE), stale, now=NOW, policy=policy())
    assert result.verdict is EligibilityVerdict.NO_LAUNCH_INSUFFICIENT_DATA
    assert result.snapshot is not None
    assert "cannot show what was added after it was fetched" in result.snapshot.reasons[-1]


def test_a_conditional_zone_needs_the_condition_recorded() -> None:
    yellow = restriction(restriction_id="yel-1", kind=RestrictionKind.YELLOW)
    unsatisfied = evaluate_route(route(), snapshot(yellow), now=NOW, policy=policy())
    assert unsatisfied.verdict is EligibilityVerdict.ALLOWED_WITH_CONDITION
    assert unsatisfied.launch_permitted is False
    assert unsatisfied.outstanding_conditions == ("yellow_coordination_recorded",)

    satisfied = evaluate_route(
        route(),
        snapshot(yellow),
        now=NOW,
        policy=policy(),
        conditions_satisfied=frozenset({"yellow_coordination_recorded"}),
    )
    assert satisfied.verdict is EligibilityVerdict.ALLOWED_WITH_CONDITION
    assert satisfied.launch_permitted is True
    assert satisfied.outstanding_conditions == ()


def test_a_facility_outage_is_a_condition_never_a_removal_of_the_volume() -> None:
    outage = restriction(
        restriction_id="out-1", kind=RestrictionKind.FACILITY_OUTAGE, footprint=buffered_footprint()
    )
    result = evaluate_route(
        route(((30.74, 76.76), (30.745, 76.78))), snapshot(outage), now=NOW, policy=policy()
    )
    assert result.verdict is EligibilityVerdict.ALLOWED_WITH_CONDITION
    assert result.outstanding_conditions == ("alternate_facility_recorded",)


def test_no_recorded_condition_can_open_a_red_zone() -> None:
    result = evaluate_route(
        route(),
        snapshot(restriction()),
        now=NOW,
        policy=policy(),
        conditions_satisfied=frozenset({"everything_we_could_write_down"}),
    )
    assert result.verdict is EligibilityVerdict.BLOCKED_NOW


def test_mixed_altitude_references_fail_closed_instead_of_guessing_a_datum() -> None:
    amsl = route(altitude_reference=AltitudeReference.AMSL)
    result = evaluate_route(amsl, snapshot(restriction()), now=NOW, policy=policy())
    assert result.verdict is EligibilityVerdict.NO_LAUNCH_INSUFFICIENT_DATA
    assert any("altitude reference" in reason for reason in result.reports[0].reasons)


def test_a_waypoint_without_a_reference_cannot_be_built() -> None:
    with pytest.raises(ValueError, match="I9"):
        RouteWaypoint(
            lat=30.74,
            lon=76.79,
            altitude_m=80.0,
            altitude_reference=AltitudeReference.UNSPECIFIED,
            t_s=0,
        )


def test_corridor_edges_report_which_edges_are_the_problem() -> None:
    inside = CorridorEdgeProfile(
        edge_id="edge-cross",
        waypoints=tuple(
            RouteWaypoint(
                lat=lat, lon=lon, altitude_m=80.0, altitude_reference=AltitudeReference.AGL, t_s=t
            )
            for t, (lat, lon) in enumerate(((30.72, 76.77), (30.74, 76.79), (30.76, 76.81)))
        ),
        entry_t_s=0,
        exit_t_s=600,
    )
    outside = CorridorEdgeProfile(
        edge_id="edge-clear",
        waypoints=tuple(
            RouteWaypoint(
                lat=lat, lon=lon, altitude_m=80.0, altitude_reference=AltitudeReference.AGL, t_s=t
            )
            for t, (lat, lon) in enumerate(CLEAR_ROUTE)
        ),
        entry_t_s=0,
        exit_t_s=600,
    )
    result = evaluate_corridor_edges(
        [inside, outside], snapshot(restriction()), window_start_utc=NOW, now=NOW, policy=policy()
    )
    assert result.blocked_edge_ids == ("edge-cross",)
    assert result.eligible_edge_ids == ("edge-clear",)
    assert result.usable_data is True
    assert result.overall.verdict is EligibilityVerdict.BLOCKED_NOW


def test_an_empty_edge_set_is_a_question_not_an_answer() -> None:
    with pytest.raises(AirspaceError):
        evaluate_corridor_edges(
            [], snapshot(restriction()), window_start_utc=NOW, now=NOW, policy=policy()
        )


def test_eligibility_refuses_to_be_read_as_clearance() -> None:
    result = evaluate_route(route(CLEAR_ROUTE), snapshot(), now=NOW, policy=policy())
    with pytest.raises(NotAFlightAuthorisationError):
        result.as_flight_authorisation()


def test_a_circle_footprint_blocks_only_inside_the_radius() -> None:
    near = route(((30.68, 76.79), (30.69, 76.79)))
    far = route(((30.75, 76.79), (30.76, 76.79)))
    zone = restriction(
        restriction_id="apt-1",
        kind=RestrictionKind.AIRPORT_VICINITY,
        footprint=circle_footprint(radius_m=2000),
        ceiling_m=None,
    )
    assert (
        evaluate_route(near, snapshot(zone), now=NOW, policy=policy()).verdict
        is EligibilityVerdict.ALLOWED_WITH_CONDITION
    )
    assert (
        evaluate_route(far, snapshot(zone), now=NOW, policy=policy()).verdict
        is EligibilityVerdict.ALLOWED_BY_MODEL
    )
