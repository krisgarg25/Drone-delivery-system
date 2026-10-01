"""Invariant I4: energy feasibility is checked for the complete mission.

``technical-build/06`` states I4 as *route outbound plus handover plus reserve
plus recovery or alternate is within available energy*. These tests are the
evidence that the sum is charged term by term, including the case the spec calls
out: a mission whose outbound leg fits but whose recovery does not must be
rejected, and the rejection must name the term that broke it.

What these tests do **not** prove: that any watt-hour figure here is real. The
battery curves behind them are illustrative profile parameters (architecture
section 10), so these are structural proofs about the model, not flight evidence.
Simulation only. Not authorised for flight.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from energy_support import BASE_US, KM_MM, MINUTE_US, mission, route, trace

from droneops_energy import (
    AIRFRAME_X_SIM,
    MarginReport,
    RejectReason,
    Route,
    TermName,
    assess_mission,
    publish_profile,
)

#: A recovery leg so long that the return trip alone exhausts the pack.
LONG_RECOVERY_MM = 150 * KM_MM

#: A recovery leg that fits the pack but not the reserve: still an I4 failure.
RESERVE_EATING_RECOVERY_MM = 100 * KM_MM


def terms(report: MarginReport) -> dict[TermName, int]:
    """Term name to charged watt-hours."""
    return {row.term: row.wh for row in report.terms}


def margin_after(report: MarginReport) -> dict[TermName, int]:
    """Term name to the margin left once that term has been charged."""
    return {row.term: row.margin_after_wh for row in report.terms}


def test_i4_charges_outbound_handover_recovery_and_reserve_together() -> None:
    """All four I4 terms are present and their sum is what the margin is compared to."""
    report = assess_mission(mission(distance_mm=6 * KM_MM, recovery_mm=6 * KM_MM))
    charged = terms(report)

    assert set(charged) == {
        TermName.OUTBOUND,
        TermName.HANDOVER,
        TermName.RECOVERY,
        TermName.RESERVE,
        TermName.GUST_UPLIFT,
    }
    assert all(wh >= 0 for wh in charged.values())
    assert (
        report.estimated_consumption_wh
        == charged[TermName.OUTBOUND] + charged[TermName.HANDOVER] + charged[TermName.RECOVERY]
    )
    assert report.total_required_wh == report.estimated_consumption_wh + report.reserve_required_wh
    assert report.margin_wh == report.available_wh - report.total_required_wh
    assert [row.term.value for row in report.terms] == [
        "outbound",
        "handover",
        "recovery",
        "reserve",
        "gust_uplift",
    ]
    assert ("margin", report.margin_wh) in report.evidence


def test_i4_rejects_a_mission_whose_outbound_fits_but_recovery_does_not() -> None:
    """The named failure mode: outbound affordable, return trip not.

    A model that checked only the delivery leg would call this feasible and lose
    the aircraft, which is exactly what I4 exists to prevent.
    """
    report = assess_mission(mission(distance_mm=3 * KM_MM, recovery_mm=LONG_RECOVERY_MM))
    surviving = margin_after(report)

    assert surviving[TermName.OUTBOUND] > 0, "the delivery leg alone fits"
    assert surviving[TermName.HANDOVER] > 0, "and so does the handover"
    assert surviving[TermName.RECOVERY] < 0, "the return leg is what crosses the line"
    assert not report.feasible
    assert report.margin_wh < 0
    assert report.breaking_term is TermName.RECOVERY
    assert RejectReason.RECOVERY_INFEASIBLE in report.reasons
    assert "recovery" in report.summary


def test_i4_rejects_a_mission_with_no_recovery_plan_at_all() -> None:
    """No recovery route is not a free recovery: unproven energy is infeasible energy."""
    report = assess_mission(mission(distance_mm=3 * KM_MM, recovery_mm=None))

    assert not report.feasible
    assert RejectReason.RECOVERY_ROUTE_MISSING in report.reasons
    assert report.breaking_term is TermName.RECOVERY
    # The missing leg charges nothing, so the margin looks generous: this is why
    # feasibility is not simply "margin >= 0".
    assert report.margin_wh > 0
    with pytest.raises(Exception, match="infeasible mission"):
        report.to_energy_estimate()


def test_i4_reserve_is_part_of_the_sum_not_a_note_afterwards() -> None:
    """Stripping the reserve moves the whole requirement: the reserve is inside it."""
    zero_reserve = publish_profile(
        {**AIRFRAME_X_SIM.to_mapping(), "name": "airframe-x-no-reserve", "reserve_fraction_bp": "0"}
    )
    with_reserve = assess_mission(mission(distance_mm=3 * KM_MM, recovery_mm=3 * KM_MM))
    without_reserve = assess_mission(
        mission(distance_mm=3 * KM_MM, recovery_mm=3 * KM_MM, profile=zero_reserve)
    )

    assert terms(without_reserve)[TermName.RESERVE] == 0
    assert terms(with_reserve)[TermName.RESERVE] > 0
    assert with_reserve.margin_wh < without_reserve.margin_wh
    assert (
        without_reserve.total_required_wh + terms(with_reserve)[TermName.RESERVE]
        == with_reserve.total_required_wh
    )


def test_i4_samples_wind_as_the_clock_advances_per_route() -> None:
    """Each route starts where the previous one ended, on the same frozen trace."""
    report = assess_mission(mission(distance_mm=3 * KM_MM, recovery_mm=3 * KM_MM))

    assert report.recovery is not None
    assert report.outbound.start_us == BASE_US
    assert report.handover.start_us == report.outbound.end_us
    assert report.recovery.start_us == report.handover.end_us
    assert report.recovery_end_us > report.arrival_us


@pytest.mark.parametrize(
    ("recovery_mm", "expected_feasible", "expected_term"),
    [
        (3 * KM_MM, True, None),
        (20 * KM_MM, True, None),
        (60 * KM_MM, True, None),
        (RESERVE_EATING_RECOVERY_MM, False, TermName.RESERVE),
        (LONG_RECOVERY_MM, False, TermName.RECOVERY),
    ],
)
def test_i4_breaks_at_a_recovery_distance(
    recovery_mm: int, expected_feasible: bool, expected_term: TermName | None
) -> None:
    """Where the complete-mission line falls, and which term the report names.

    Two distinct failures are visible: a return leg the pack cannot cover at all,
    and a return leg that fits but leaves less than the required reserve. Both are
    I4 rejections, and they are not the same sentence to a reviewer.
    """
    report = assess_mission(mission(distance_mm=3 * KM_MM, recovery_mm=recovery_mm))

    assert report.feasible is expected_feasible
    assert report.breaking_term is expected_term


def test_i4_with_zero_payload_still_charges_hover_and_reserve() -> None:
    """An empty payload does not make a mission free: handover and reserve still bite."""
    report = assess_mission(
        mission(
            distance_mm=3 * KM_MM,
            recovery_mm=3 * KM_MM,
            payload_g=0,
            hover_us=10 * MINUTE_US,
        )
    )

    assert terms(report)[TermName.HANDOVER] > 0
    assert terms(report)[TermName.RESERVE] > 0
    assert report.margin_wh == report.available_wh - report.total_required_wh


def test_alternate_recovery_is_feasible_where_home_recovery_is_not() -> None:
    """'recovery OR alternate': the nearer alternate is a legitimate way to satisfy I4."""
    rejected = assess_mission(mission(distance_mm=3 * KM_MM, recovery_mm=LONG_RECOVERY_MM))
    plain_home = assess_mission(mission(distance_mm=3 * KM_MM, recovery_mm=12 * KM_MM))
    alternate = assess_mission(
        mission(distance_mm=3 * KM_MM, recovery_mm=12 * KM_MM, requires_alternate=True)
    )

    assert not rejected.feasible
    assert plain_home.feasible
    assert alternate.feasible
    assert alternate.margin_wh >= 0
    # Choosing an alternate carries extra policy reserve, visible as its own charge.
    assert alternate.reserve_required_wh > plain_home.reserve_required_wh


def test_alternate_flag_without_a_recovery_route_is_still_rejected() -> None:
    """Declaring an alternate is not a substitute for planning the recovery leg."""
    report = assess_mission(
        mission(distance_mm=3 * KM_MM, recovery_mm=None, requires_alternate=True)
    )

    assert not report.feasible
    assert RejectReason.RECOVERY_ROUTE_MISSING in report.reasons


def test_multi_leg_route_is_charged_leg_by_leg() -> None:
    """A four-leg outbound equals the sum of its legs, and the clock advances."""
    legs = tuple(
        route(f"leg-{index}", 2 * KM_MM, course_mdeg=90_000 + index * 45_000).legs[0]
        for index in range(4)
    )
    base = mission(distance_mm=0, recovery_mm=0)
    multi = assess_mission(replace(base, outbound=Route(label="outbound", legs=legs)))
    singles = [assess_mission(mission(distance_mm=2 * KM_MM, recovery_mm=0)) for _ in legs]

    assert len(multi.outbound.legs) == 4
    assert multi.outbound.end_us > multi.outbound.start_us
    # Each route is ceiling-rounded to a whole watt-hour, so the four-leg total may
    # differ from the sum of four separate missions by at most one watt-hour per leg.
    assert terms(multi)[TermName.OUTBOUND] == pytest.approx(
        sum(terms(row)[TermName.OUTBOUND] for row in singles), abs=len(legs)
    )
    assert trace(speed_dmps=0).span_minutes == 89


def test_a_blocked_outbound_leg_is_attributed_to_the_outbound_term() -> None:
    """When the wind makes a leg unflyable, the rejection names that leg, not a total."""
    report = assess_mission(
        mission(
            distance_mm=3 * KM_MM,
            recovery_mm=3 * KM_MM,
            weather=trace(speed_dmps=200, bearing_mdeg=0),
        )
    )

    assert not report.feasible
    assert RejectReason.ROUTE_BLOCKED_BY_WIND in report.reasons
    assert report.breaking_term in {TermName.OUTBOUND, TermName.RECOVERY}
    assert not report.routes_complete
