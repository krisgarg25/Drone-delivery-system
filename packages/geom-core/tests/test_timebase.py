"""Unit tests for the integer-microsecond time base."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from droneops_geom_core import (
    DATETIME_REPRESENTABLE_MIN_US,
    EPOCH,
    INT64_MICROSECOND_MAX,
    INT64_MICROSECOND_MIN,
    MICROS_PER_DAY,
    MICROS_PER_SECOND,
    Microsecond,
    TimeDomainError,
    as_duration_us,
    as_microseconds,
    checked_add,
    datetimes_to_microseconds,
    describe_span,
    from_microseconds,
    saturating_add,
    to_microseconds,
)


def test_epoch_is_timezone_aware_utc() -> None:
    assert EPOCH.tzinfo is UTC
    assert to_microseconds(EPOCH) == 0


def test_round_trip_through_datetime_is_exact() -> None:
    for value in (0, 1, -1, MICROS_PER_DAY, -MICROS_PER_DAY, 123_456_789):
        moment = from_microseconds(value)
        assert moment.tzinfo is UTC
        assert to_microseconds(moment) == value


def test_conversions_are_inverse_in_the_other_direction() -> None:
    for moment in (
        datetime(2026, 9, 30, 12, 0, 0, tzinfo=UTC),
        datetime(1999, 1, 1, 0, 0, 0, 1, tzinfo=UTC),
        datetime(2026, 10, 1, 23, 59, 59, 999_999, tzinfo=UTC),
    ):
        assert from_microseconds(to_microseconds(moment)) == moment


def test_offset_datetimes_map_to_the_same_instant() -> None:
    """A non-UTC offset is the same instant, so it must convert identically."""
    plus_two = timezone(timedelta(hours=2))
    utc = datetime(2026, 9, 30, 10, 0, tzinfo=UTC)
    shifted = utc.astimezone(plus_two)
    assert to_microseconds(utc) == to_microseconds(shifted)


def test_naive_datetime_is_rejected_never_assumed() -> None:
    with pytest.raises(TimeDomainError, match="timezone-aware"):
        to_microseconds(datetime(2026, 9, 30, 10, 0))


def test_float_and_bool_are_not_time() -> None:
    for bad in (1.0, True, False, "1000", None):
        with pytest.raises(TimeDomainError):
            as_microseconds(bad)


def test_decimal_is_not_time() -> None:
    from decimal import Decimal

    with pytest.raises(TimeDomainError):
        as_microseconds(Decimal("1000"))


def test_int64_domain_is_enforced() -> None:
    assert as_microseconds(INT64_MICROSECOND_MIN) == INT64_MICROSECOND_MIN
    assert as_microseconds(INT64_MICROSECOND_MAX) == INT64_MICROSECOND_MAX
    with pytest.raises(TimeDomainError):
        as_microseconds(INT64_MICROSECOND_MAX + 1)
    with pytest.raises(TimeDomainError):
        as_microseconds(INT64_MICROSECOND_MIN - 1)


def test_negative_time_is_a_real_instant_before_the_epoch() -> None:
    assert to_microseconds(EPOCH - timedelta(microseconds=1)) == -1
    assert from_microseconds(-MICROS_PER_SECOND) == datetime(1999, 12, 31, 23, 59, 59, tzinfo=UTC)


def test_checked_add_rejects_a_step_out_of_domain() -> None:
    assert checked_add(5, -10) == -5
    with pytest.raises(TimeDomainError):
        checked_add(INT64_MICROSECOND_MAX, 1)


def test_saturating_add_clamps_instead_of_raising() -> None:
    assert saturating_add(INT64_MICROSECOND_MAX, 1) == INT64_MICROSECOND_MAX
    assert saturating_add(INT64_MICROSECOND_MIN, -1) == INT64_MICROSECOND_MIN
    assert saturating_add(10, -25) == -15


def test_duration_must_be_non_negative() -> None:
    assert as_duration_us(0) == 0
    with pytest.raises(TimeDomainError, match="non-negative"):
        as_duration_us(-1)


def test_datetime_pair_conversion_rejects_a_reversed_window() -> None:
    late = datetime(2026, 1, 2, tzinfo=UTC)
    early = datetime(2026, 1, 1, tzinfo=UTC)
    assert datetimes_to_microseconds(early, late) == (
        to_microseconds(early),
        to_microseconds(late),
    )
    with pytest.raises(TimeDomainError, match="precedes"):
        datetimes_to_microseconds(late, early)


def test_rendering_beyond_the_datetime_window_is_a_clean_rejection() -> None:
    """The int64 domain is wider than ``datetime``; the error must say so."""
    assert DATETIME_REPRESENTABLE_MIN_US > INT64_MICROSECOND_MIN
    with pytest.raises(TimeDomainError, match="cannot be rendered"):
        from_microseconds(INT64_MICROSECOND_MIN)
    with pytest.raises(TimeDomainError, match="cannot be rendered"):
        from_microseconds(INT64_MICROSECOND_MAX)


def test_describe_span_is_integer_exact_and_signed() -> None:
    assert describe_span(3_600_000_000 + 61_000_000 + 500) == "01:01:01.000500"
    assert describe_span(-500_000) == "-00:00:00.500000"
    assert describe_span(0) == "00:00:00.000000"


def test_microsecond_is_a_value_not_a_container() -> None:
    """The newtype is erased at runtime: a Microsecond *is* an int."""
    assert isinstance(Microsecond(7), int)
    assert Microsecond(7) + 1 == 8
