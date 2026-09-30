import uuid
from datetime import UTC, datetime

import pytest

from droneops_conflict_checker import check_interval_overlap
from droneops_contracts import MissionStatus, PriorityClass, ReservationState
from droneops_core_domain import create_simulated_mission, transition_mission
from droneops_reservations import Reservation, canonical_resource_key, validate_commit_set
from droneops_sim_core import DeterministicEventQueue
from droneops_telemetry import Observation

NOW = datetime(2026, 9, 30, tzinfo=UTC)
LATER = datetime(2026, 9, 30, 1, tzinfo=UTC)
EVEN_LATER = datetime(2026, 9, 30, 2, tzinfo=UTC)


def test_mission_transition_blocks_authority_skips() -> None:
    mission = create_simulated_mission(uuid.uuid4(), uuid.uuid4(), "a", "b", PriorityClass.ROUTINE, NOW)
    with pytest.raises(ValueError):
        transition_mission(mission, MissionStatus.ACTIVATED)


def test_committed_time_overlap_is_detected() -> None:
    left = Reservation(uuid.uuid4(), uuid.uuid4(), canonical_resource_key("corridor", "7"), (NOW, EVEN_LATER), ReservationState.COMMITTED)
    right = Reservation(uuid.uuid4(), uuid.uuid4(), left.resource_key, (LATER, EVEN_LATER), ReservationState.COMMITTED)
    with pytest.raises(ValueError, match="rejected"):
        validate_commit_set([left, right], checker=check_interval_overlap)


def test_independent_checker_is_more_conservative_than_no_overlap() -> None:
    assert check_interval_overlap((NOW, LATER), (LATER, EVEN_LATER)) == "SAFE"
    assert check_interval_overlap((NOW, EVEN_LATER), (LATER, EVEN_LATER)) == "CONFLICT"


def test_simulation_events_execute_in_integer_time_order() -> None:
    queue = DeterministicEventQueue()
    executed: list[str] = []
    queue.schedule(200, "second", lambda _: executed.append("second"))
    queue.schedule(100, "first", lambda _: executed.append("first"))
    assert queue.advance(150) == ["first"]
    assert queue.advance(250) == ["second"]


def test_unlisted_telemetry_is_not_accepted() -> None:
    observation = Observation(
        aircraft_id="aircraft-1",
        message_type="MAV_CMD",
        measurement_time=NOW,
        received_time=NOW,
    )
    assert observation.accepted() is False
