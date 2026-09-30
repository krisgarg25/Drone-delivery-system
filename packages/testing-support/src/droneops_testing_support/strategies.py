"""Hypothesis strategies and seeded RNG for contract property tests.

Hypothesis is an optional import: this module always imports cleanly, but the
``*_strategy`` callables require ``hypothesis`` to be installed and raise an
informative error otherwise. Strategies only ever build UTC-aware datetimes so a
property test can never accidentally produce a naive timestamp (I10).
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

UTC = UTC

try:  # pragma: no cover - exercised only when hypothesis is installed
    from hypothesis import strategies as st

    _HAS_HYPOTHESIS = True
except Exception:
    st = None  # type: ignore[assignment]
    _HAS_HYPOTHESIS = False


def require_hypothesis() -> None:
    if not _HAS_HYPOTHESIS:
        raise RuntimeError(
            "hypothesis is required for these strategies but is not installed; "
            "add it to the dev group (uv add --group dev hypothesis)."
        )


def seeded_random(seed: int) -> random.Random:
    """A recorded random stream; the seed is always explicit (conventions 4/10)."""

    return random.Random(seed)


def utc_datetimes(*, min_year: int = 2024, max_year: int = 2030):
    require_hypothesis()
    return st.datetimes(
        min_value=datetime(min_year, 1, 1),
        max_value=datetime(max_year, 12, 31),
        timezones=st.just(UTC),
    )


def altitude_references():
    require_hypothesis()
    # UNSPECIFIED is never drawn: strategies build valid positions (I9).
    return st.sampled_from(["AGL", "AMSL", "GROUND", "PAD"])


def positions():
    require_hypothesis()
    return st.builds(
        _make_position,
        lat=st.floats(-90.0, 90.0, allow_nan=False),
        lon=st.floats(-180.0, 180.0, allow_nan=False),
        altitude_m=st.floats(-100.0, 500.0, allow_nan=False),
        altitude_reference=altitude_references(),
        uncertainty_m=st.one_of(st.none(), st.floats(0.0, 50.0, allow_nan=False)),
    )


def _make_position(**kw: object):
    from droneops_contracts import Position

    return Position(**kw)  # type: ignore[arg-type]


def time_windows():
    require_hypothesis()
    return st.builds(
        _make_window,
        start=utc_datetimes(),
        span_s=st.integers(min_value=1, max_value=7200),
    )


def _make_window(start: datetime, span_s: int):
    from droneops_contracts import TimeWindow

    return TimeWindow(start_utc=start, end_utc=start + timedelta(seconds=span_s))


def order_requests():
    require_hypothesis()
    return st.builds(
        _make_order,
        origin=st.sampled_from(["hub-north", "hub-south"]),
        destination=st.sampled_from(["node-hospital-a", "node-clinic-b"]),
        deadline_span=st.integers(min_value=1, max_value=86_400),
        key=st.uuids().map(str),
    )


def _make_order(origin: str, destination: str, deadline_span: int, key: str):
    from droneops_contracts import Order, OrderStatus, PriorityClass

    from .factories import hex64

    base = datetime(2026, 10, 1, tzinfo=UTC)
    return Order(
        id=uuid_from_str(key),
        tenant_id=uuid_from_str(key),
        external_ref_hash=hex64(abs(hash(origin + destination)) % (2**64)),
        origin_facility_id=origin,
        destination_facility_id=destination,
        payload_profile_id="diagnostic-kit-v1",
        priority_class=PriorityClass.ROUTINE,
        earliest_departure=base,
        requested_deadline=base + timedelta(seconds=deadline_span),
        status=OrderStatus.RECEIVED,
        idempotency_key=key,
    )


def uuid_from_str(value: str):
    import uuid

    return uuid.uuid5(uuid.NAMESPACE_URL, value)



def resource_keys():
    require_hypothesis()
    kinds = ["corridor", "volume_cell", "crossing", "pad", "charger_bay", "crew"]
    return st.builds(
        lambda kind, name: f"{kind}:{name}",
        kind=st.sampled_from(kinds),
        name=st.text(min_size=1, max_size=16, alphabet=st.characters(exclude_characters=(",",))),
    )


def telemetry_sequences():
    require_hypothesis()
    return st.integers(min_value=0, max_value=1_000_000)
