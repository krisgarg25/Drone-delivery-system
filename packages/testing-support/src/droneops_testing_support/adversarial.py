"""Adversarial value builders.

These produce the hostile inputs named in ``technical-build/06-testing-and-safety.md``
section 3. Each builder returns a dict of field overrides meant to be applied to
the matching factory in ``factories``. Values marked REJECTED are caught by a
contract validator; values marked ACCEPTED are legal-but-hostile boundaries or
cross-object conditions that a downstream ledger/checker must handle.
"""

from __future__ import annotations

from datetime import timedelta

from .factories import (
    BASE_DT,
    UUID_MISSION,
    make_reservation,
    make_telemetry_observation,
    utc_offset,
)


def zero_energy_kwargs() -> dict[str, object]:
    """ACCEPTED boundary: a plan reporting zero energy — margin and consumption 0."""

    return {
        "profile_id": "airframe-x-sim",
        "estimated_consumption_wh": 0,
        "reserve_required_wh": 0,
        "margin_wh": 0,
    }


def zero_battery_state_kwargs() -> dict[str, object]:
    """ACCEPTED boundary: an aircraft at literally zero remaining energy."""

    return {"battery_pct": 0.0, "energy_wh_remaining": 0}


def impossible_deadline_kwargs() -> dict[str, object]:
    """REJECTED: a requested deadline at or before the earliest departure."""

    return {
        "earliest_departure": BASE_DT,
        "requested_deadline": BASE_DT - timedelta(seconds=1),
    }


def crossing_times_reservations() -> tuple[object, object]:
    """Two committed reservations on the same corridor with overlapping windows.

    Both are individually valid; only the reservation ledger / independent checker
    may reject the overlap (invariant I1).
    """

    left = make_reservation(
        id=UUID_MISSION,
        resource_key="corridor:shared:segment-1",
        resource_kind="corridor",
        valid_from=BASE_DT,
        valid_until=utc_offset(600),
        state="committed",
    )
    right = make_reservation(
        resource_key="corridor:shared:segment-1",
        resource_kind="corridor",
        valid_from=utc_offset(300),
        valid_until=utc_offset(900),
        state="committed",
    )
    return left, right


def clock_jump_kwargs() -> dict[str, object]:
    """REJECTED: receipt time jumps before the measurement time (I10)."""

    return {
        "measurement_time": utc_offset(100),
        "received_time": utc_offset(0),
    }


def reversed_altitude_kwargs() -> dict[str, object]:
    """REJECTED: a position whose altitude reference is UNSPECIFIED (I9).

    Also used to represent an inverted altitude profile — an altitude carried
    without any reference is never a valid position.
    """

    return {"altitude_reference": "UNSPECIFIED", "altitude_m": -50.0}


def duplicated_sequence_kwargs() -> dict[str, object]:
    """A second observation with the same stream sequence as the default.

    Sequence uniqueness is a per-stream property enforced by the ordering
    projector, so this value is ACCEPTED by the model but must be quarantined as
    out_of_order downstream (I10).
    """

    return {"sequence": make_telemetry_observation().sequence}


def stale_aircraft_position_kwargs() -> dict[str, object]:
    """REJECTED: uncertainty reported while retaining nothing (I3)."""

    return {
        "state": "uncertain",
        "last_accepted_measurement_time": BASE_DT,
        "staleness_s": 120,
        "protected_volume_retained": False,
    }
