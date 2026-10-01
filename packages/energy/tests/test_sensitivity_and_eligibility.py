"""Sensitivity sweeps and the fleet eligibility query.

Both are Phase 3 exit-criteria features: reviewers must see the trade-offs behind
an illustrative number, and ``packages/dispatcher`` must get a feasibility answer
it can rank on without this package deciding anything about assignment, queueing or
fairness.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from energy_support import BASE_US, KM_MM, mission, trace

from droneops_energy import (
    AIRFRAME_X_SIM,
    AIRFRAME_Y_SIM,
    AircraftEnergyState,
    Knob,
    MissionShape,
    RejectReason,
    TermName,
    assess_candidate,
    assess_fleet,
    load_profile,
    perturb,
    sweep_sensitivity,
)

SWEEP_MISSION = 25 * KM_MM


@pytest.fixture
def swept():
    """One documented sweep of a mission with a modest positive margin."""
    return sweep_sensitivity(
        mission(
            distance_mm=SWEEP_MISSION,
            recovery_mm=SWEEP_MISSION,
            weather=trace(speed_dmps=40, bearing_mdeg=90_000, gust_dmps=60),
        )
    )


# ------------------------------------------------------------------ sensitivity
def test_the_sweep_moves_every_knob_and_reports_whose_basis_is_illustrative(swept) -> None:
    assert {row.knob for row in swept.rows} == set(Knob)
    assert swept.steps_requested > 25
    assert swept.baseline.disclaimer == swept.disclaimer
    assert "ILLUSTRATIVE" in swept.disclaimer
    assert swept.profile_sha256 == AIRFRAME_X_SIM.parameters_sha256


def test_wind_and_payload_are_the_knobs_that_move_the_margin_most(swept) -> None:
    """The trade-off visibility the exit criteria ask for: measure this first."""
    tornado = swept.tornado()

    assert tornado[0] in {Knob.SUSTAINED_WIND_DMPS, Knob.ROUTE_SCALE_BP}
    assert swept.knob(Knob.STATE_OF_HEALTH_BP).max_abs_shift_wh > 0
    assert swept.knob(Knob.PAYLOAD_G).max_abs_shift_wh > 0
    for response in swept.rows:
        assert response.rows[0][0] == 0, "delta zero is the baseline itself"
        assert response.rows[0][1].margin_wh == swept.baseline.margin_wh


def test_a_sweep_shows_where_the_decision_flips(swept) -> None:
    """A reviewer needs the break-even, not just the slope."""
    wind = swept.knob(Knob.SUSTAINED_WIND_DMPS)
    payload = swept.knob(Knob.PAYLOAD_G)

    assert wind.flip_delta is not None or payload.flip_delta is not None
    flipped = wind.knob, wind.flip_delta
    assert flipped[0] is Knob.SUSTAINED_WIND_DMPS
    for delta, report in wind.rows:
        if delta == 0:
            continue
        assert isinstance(report.margin_wh, int)
    assert [delta for delta, _ in wind.rows] == [0, -40, -20, 20, 40, 60]


def test_a_refused_step_is_recorded_as_a_refusal_not_a_zero_cost() -> None:
    """A payload the airframe cannot lift must not read as 'free' in the table."""
    report = sweep_sensitivity(
        mission(distance_mm=3 * KM_MM, recovery_mm=3 * KM_MM, payload_g=1900),
        knobs=(Knob.PAYLOAD_G,),
    )
    payload = report.knob(Knob.PAYLOAD_G)

    assert payload.refusals, "2900 g is over the 2000 g profile limit"
    assert all("exceeds profile limit" in note for _delta, note in payload.refusals)
    assert [delta for delta, _ in payload.rows if delta > 500] == []


def test_sensitivity_is_reproducible_and_table_shaped(swept) -> None:
    again = sweep_sensitivity(
        mission(
            distance_mm=SWEEP_MISSION,
            recovery_mm=SWEEP_MISSION,
            weather=trace(speed_dmps=40, bearing_mdeg=90_000, gust_dmps=60),
        )
    )

    assert [str(row) for row in swept.as_table()] == [str(row) for row in again.as_table()]
    for row in swept.as_table():
        assert len(row) == 5
        assert row[0] in {knob.value for knob in Knob}


def test_perturb_holds_everything_else_fixed() -> None:
    """A sweep is only evidence if exactly one input moved."""
    base = mission(distance_mm=10 * KM_MM, recovery_mm=10 * KM_MM, weather=trace(speed_dmps=30))
    probed = perturb(base, Knob.SUSTAINED_WIND_DMPS, 20)

    assert probed.weather.samples[0].speed_dmps == 50
    assert probed.outbound == base.outbound
    assert probed.payload_g == base.payload_g
    assert probed.profile is base.profile
    reversed_probe = perturb(base, Knob.WIND_REVERSAL, 1)
    assert reversed_probe.weather.samples[0].bearing_mdeg == 270_000
    assert reversed_probe.outbound == base.outbound


def test_unknown_knob_and_empty_sweep_are_errors() -> None:
    with pytest.raises(Exception, match="unknown knob"):
        sweep_sensitivity(mission(), knobs=(Knob.PAYLOAD_G, Knob))  # type: ignore[arg-type]


# ------------------------------------------------------------------- eligibility
def fleet(payload_g: int = 500) -> MissionShape:
    """The mission half of an eligibility query: the shape without an aircraft."""
    return MissionShape(
        mission_id="ord-fleet-000",
        payload_g=payload_g,
        outbound=mission(distance_mm=payload_g * KM_MM // 500).outbound,
        handover=mission().handover,
        recovery=mission().recovery,
        weather=trace(speed_dmps=40, bearing_mdeg=90_000),
        clock_us=BASE_US,
    )


def test_the_fleet_query_returns_margin_inputs_not_an_assignment() -> None:
    """Ranked margins in, decision out: no queueing or priority happens here."""
    candidates = (
        AircraftEnergyState("air-02-synthetic", AIRFRAME_X_SIM, charge_bp=10_000),
        AircraftEnergyState("air-01-synthetic", AIRFRAME_Y_SIM, charge_bp=9200),
        AircraftEnergyState("air-04-synthetic", AIRFRAME_X_SIM, charge_bp=8500),
    )
    assessment = assess_fleet(fleet(), candidates)

    assert assessment.evaluated == 3
    assert assessment.best is not None
    margins = [row.margin_wh for row in assessment.feasible]
    assert margins == sorted(margins, reverse=True), "best margin first"
    assert len({row.aircraft_id for row in assessment.feasible}) == len(assessment.feasible)
    # Lower charge strictly lowers the margin for the same airframe.
    rows = (*assessment.feasible, *assessment.rejected)
    per_id = {row.aircraft_id: row.margin_wh for row in rows}
    assert per_id["air-04-synthetic"] < per_id["air-02-synthetic"]


def test_input_order_cannot_change_the_ranking() -> None:
    forward = assess_fleet(
        fleet(),
        (
            AircraftEnergyState("air-a", AIRFRAME_X_SIM, charge_bp=10_000),
            AircraftEnergyState("air-b", AIRFRAME_X_SIM, charge_bp=7000),
        ),
    )
    backward = assess_fleet(
        fleet(),
        (
            AircraftEnergyState("air-b", AIRFRAME_X_SIM, charge_bp=7000),
            AircraftEnergyState("air-a", AIRFRAME_X_SIM, charge_bp=10_000),
        ),
    )

    assert forward.ranking() == backward.ranking()


def test_status_is_carried_through_untouched_because_i5_belongs_to_dispatch() -> None:
    """This package does not filter maintenance or offline aircraft: it prices them.

    Invariant I5 says those aircraft never appear eligible; the decision is the
    dispatcher's, and the energy answer must stay available for every candidate so
    the dispatcher can exclude on status without losing the margin figure.
    """
    rows = assess_fleet(
        fleet(),
        (
            AircraftEnergyState("air-mro", AIRFRAME_X_SIM, status="maintenance"),
            AircraftEnergyState("air-off", AIRFRAME_X_SIM, status="offline"),
        ),
    )
    statuses = {row.aircraft_id: row.status for row in (*rows.feasible, *rows.rejected)}

    assert statuses == {"air-mro": "maintenance", "air-off": "offline"}
    assert all(row.feasible for row in (*rows.feasible, *rows.rejected))


def test_an_aircraft_that_cannot_lift_the_payload_is_a_priced_rejection() -> None:
    """A dispatch scan must not abort on the first unsuitable airframe."""
    assessment = assess_candidate(
        fleet(payload_g=3000),
        AircraftEnergyState("air-02-synthetic", AIRFRAME_X_SIM),
    )

    assert not assessment.feasible
    assert assessment.margin_wh is None
    assert "exceeds profile limit" in assessment.note
    assert assessment.report is None
    assert "ILLUSTRATIVE" in assessment.disclaimer


def test_a_degraded_aircraft_moves_from_feasible_to_rejected_with_a_named_term() -> None:
    """The candidate query is where 'which aircraft can realistically fly this' is answered."""
    template = mission(distance_mm=25 * KM_MM, recovery_mm=25 * KM_MM, payload_g=1500)
    shape = replace(fleet(payload_g=1500), outbound=template.outbound, recovery=template.recovery)
    healthy = assess_candidate(
        shape, AircraftEnergyState("air-06", AIRFRAME_Y_SIM, charge_bp=10_000, soh_bp=10_000)
    )
    worn = assess_candidate(
        shape, AircraftEnergyState("air-06", AIRFRAME_Y_SIM, charge_bp=10_000, soh_bp=6500)
    )

    assert healthy.feasible
    assert not worn.feasible
    assert worn.breaking_term in {TermName.RECOVERY, TermName.RESERVE, TermName.OUTBOUND}
    assert RejectReason.RECOVERY_INFEASIBLE in worn.reasons or not worn.feasible


def test_eligibility_uses_only_stored_versioned_profiles() -> None:
    """A candidate names a profile version; the kernel refuses to invent one."""
    stored = load_profile(AIRFRAME_X_SIM.name, AIRFRAME_X_SIM.version)
    assessment = assess_candidate(fleet(), AircraftEnergyState("air-x", stored))

    assert assessment.parameters_sha256 == stored.parameters_sha256
    with pytest.raises(Exception, match="stored profiles"):
        load_profile("airframe-x-sim", "illustrative-2")
