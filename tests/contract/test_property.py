"""Property tests over contract strategies (hypothesis)."""

from __future__ import annotations

from datetime import UTC

from hypothesis import HealthCheck, given, settings

from droneops_contracts import AltitudeReference, Position, TimeWindow
from droneops_testing_support import seeded_random, strategies

UTC = UTC


@given(order=strategies.order_requests())
@settings(max_examples=80, suppress_health_check=[HealthCheck.too_slow])
def test_generated_orders_always_have_a_feasible_deadline(order) -> None:
    assert order.requested_deadline > order.earliest_departure
    assert order.tenant_id is not None
    assert order.idempotency_key


@given(position=strategies.positions())
@settings(max_examples=80, suppress_health_check=[HealthCheck.too_slow])
def test_generated_positions_are_never_unspecified_altitude(position: Position) -> None:
    assert position.altitude_reference is not AltitudeReference.UNSPECIFIED
    assert -90.0 <= position.lat <= 90.0


@given(window=strategies.time_windows())
@settings(max_examples=80, suppress_health_check=[HealthCheck.too_slow])
def test_generated_windows_are_ordered_and_utc(window: TimeWindow) -> None:
    assert window.end_utc > window.start_utc
    assert window.start_utc.tzinfo is not None
    assert window.start_utc.utcoffset().total_seconds() == 0


@given(key=strategies.resource_keys())
@settings(max_examples=80)
def test_generated_resource_keys_are_allowed_kinds(key: str) -> None:
    from droneops_contracts import validate_resource_key

    assert validate_resource_key(key) == key


def test_seeded_random_is_reproducible() -> None:
    a = [seeded_random(93214).random() for _ in range(5)]
    b = [seeded_random(93214).random() for _ in range(5)]
    assert a == b


def test_seeded_random_differs_across_seeds() -> None:
    assert seeded_random(1).random() != seeded_random(2).random()
