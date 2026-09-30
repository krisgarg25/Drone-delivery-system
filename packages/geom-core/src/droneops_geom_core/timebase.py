"""Integer-microsecond time base, and the only datetime boundary in the package.

conventions.md section 4 and ``technical-build/11`` section 8 require that the
kernel never reads a wall clock and never does float arithmetic on time. Every
temporal quantity in geom-core is therefore :data:`Microsecond`: an ``int``
count of microseconds relative to :data:`EPOCH`.

Rules this module enforces
    * ``int`` only. ``bool``, ``float``, ``Decimal`` and ``datetime`` are all
      rejected as raw time values, so a unit error cannot masquerade as a
      plausible timestamp.
    * Negative values are legal: they are instants before :data:`EPOCH`, and a
      scenario that anchors a plan at ``T-0`` needs them.
    * ``datetime`` crosses only through :func:`to_microseconds` /
      :func:`from_microseconds`, both of which demand or return a
      **timezone-aware UTC** value. A naive ``datetime`` is ambiguous and is
      rejected rather than assumed to be local time (invariant I10).
    * Every returned sequence is sorted; nothing here reads a clock, an
      environment variable or a locale.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Final, NewType

from .errors import TimeDomainError

Microsecond = NewType("Microsecond", int)
"""Integer microseconds since :data:`EPOCH`.

A duration uses the same type and must be non-negative. The distinction
between an instant and a duration is carried by the field name
(``*_start`` / ``*_us``) and validated at construction, not by a second type
that would force a cast on every arithmetic step in the hot path.
"""

MICROS_PER_MILLISECOND: Final[int] = 1_000
MICROS_PER_SECOND: Final[int] = 1_000_000
MICROS_PER_MINUTE: Final[int] = 60 * MICROS_PER_SECOND
MICROS_PER_HOUR: Final[int] = 60 * MICROS_PER_MINUTE
MICROS_PER_DAY: Final[int] = 24 * MICROS_PER_HOUR

INT64_MICROSECOND_MIN: Final[int] = -(2**63)
INT64_MICROSECOND_MAX: Final[int] = 2**63 - 1
"""PostgreSQL ``timestamptz(6)`` and the reservation ledger both store ``int64``
microseconds, so geom-core refuses to produce a value the ledger cannot hold."""

UTC: Final[timezone] = UTC
EPOCH: Final[datetime] = datetime(2000, 1, 1, 0, 0, 0, tzinfo=UTC)
"""Frozen epoch. Changing it changes every stored key, so it is a constant.

Deliberately not 1970: it keeps synthetic scenario values small and readable in
decision traces while still being a fixed, documented anchor.
"""

def _delta_microseconds(delta: timedelta) -> int:
    """Exact microsecond count of a ``timedelta`` with no float anywhere."""
    return delta.days * MICROS_PER_DAY + delta.seconds * MICROS_PER_SECOND + delta.microseconds


_DATETIME_MIN: Final[datetime] = datetime(1, 1, 1, 0, 0, 0, tzinfo=UTC)
_DATETIME_MAX: Final[datetime] = datetime(9999, 12, 31, 23, 59, 59, 999_999, tzinfo=UTC)
DATETIME_REPRESENTABLE_MIN_US: Final[int] = _delta_microseconds(_DATETIME_MIN - EPOCH)
DATETIME_REPRESENTABLE_MAX_US: Final[int] = _delta_microseconds(_DATETIME_MAX - EPOCH)
"""The sub-range of :data:`Microsecond` that round-trips to a ``datetime``.

The int64 domain is wider than Python's ``datetime`` on the past side; callers
that must render a value externally get this clean rejection instead of an
``OverflowError`` from the standard library.
"""


def as_microseconds(value: object) -> Microsecond:
    """Validate and coerce an arbitrary runtime value to :data:`Microsecond`.

    Raises:
        TimeDomainError: value is not an ``int`` (``bool`` included), or lies
            outside the int64 microsecond domain.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise TimeDomainError(f"time must be an int count of microseconds, got {type(value).__name__}")
    if value < INT64_MICROSECOND_MIN or value > INT64_MICROSECOND_MAX:
        raise TimeDomainError(f"time {value} us is outside the int64 microsecond domain")
    return Microsecond(value)


def as_duration_us(value: object) -> Microsecond:
    """:func:`as_microseconds` for a duration: non-negative by definition."""
    microseconds = as_microseconds(value)
    if microseconds < 0:
        raise TimeDomainError(f"duration must be non-negative, got {microseconds} us")
    return microseconds


def checked_add(base: int, delta: int) -> Microsecond:
    """Add two microsecond quantities, rejecting a step outside the domain."""
    total = base + delta
    if total < INT64_MICROSECOND_MIN or total > INT64_MICROSECOND_MAX:
        raise TimeDomainError(f"{base} + {delta} leaves the int64 microsecond domain")
    return Microsecond(total)


def saturating_add(base: int, delta: int) -> Microsecond:
    """Add and clamp to the domain edges instead of raising.

    Used only for padding a *blocked* window: saturation is conservative there,
    because a window that reaches the domain edge blocks more of the timeline,
    never less. Never use this for an instant an aircraft is claimed to occupy.
    """
    total = base + delta
    if total < INT64_MICROSECOND_MIN:
        return Microsecond(INT64_MICROSECOND_MIN)
    if total > INT64_MICROSECOND_MAX:
        return Microsecond(INT64_MICROSECOND_MAX)
    return Microsecond(total)


def microseconds_from_seconds(seconds: int) -> Microsecond:
    """Exact conversion from whole seconds. Floats are rejected: 0.1 s is not 1."""
    if isinstance(seconds, bool) or not isinstance(seconds, int):
        raise TimeDomainError(f"seconds must be an int, got {type(seconds).__name__}")
    return as_microseconds(seconds * MICROS_PER_SECOND)


def microseconds_from_milliseconds(milliseconds: int) -> Microsecond:
    """Exact conversion from whole milliseconds."""
    if isinstance(milliseconds, bool) or not isinstance(milliseconds, int):
        raise TimeDomainError(f"milliseconds must be an int, got {type(milliseconds).__name__}")
    return as_microseconds(milliseconds * MICROS_PER_MILLISECOND)


def to_microseconds(moment: datetime) -> Microsecond:
    """Convert a timezone-aware ``datetime`` to kernel time.

    Raises:
        TimeDomainError: ``moment`` is naive (its offset is ambiguous), is not
            a ``datetime``, or converts outside the int64 domain.
    """
    if not isinstance(moment, datetime):
        raise TimeDomainError(f"expected a datetime, got {type(moment).__name__}")
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise TimeDomainError("datetime must be timezone-aware; naive time is ambiguous (I10)")
    delta = moment.astimezone(UTC) - EPOCH
    return as_microseconds(_delta_microseconds(delta))


def from_microseconds(count: int) -> datetime:
    """Render kernel time as a timezone-aware UTC ``datetime``.

    Display, export and adapter boundaries only. Do not feed the result back
    into arithmetic: convert, compare and decide in :data:`Microsecond`.

    Raises:
        TimeDomainError: value is outside the datetime-representable window.
    """
    microseconds = as_microseconds(count)
    if microseconds < DATETIME_REPRESENTABLE_MIN_US or microseconds > DATETIME_REPRESENTABLE_MAX_US:
        raise TimeDomainError(
            f"time {microseconds} us cannot be rendered as a datetime; "
            f"window is [{DATETIME_REPRESENTABLE_MIN_US}, {DATETIME_REPRESENTABLE_MAX_US}]"
        )
    return EPOCH + timedelta(microseconds=microseconds)


def datetimes_to_microseconds(
    start: datetime, end: datetime
) -> tuple[Microsecond, Microsecond]:
    """Boundary helper that converts a half-open ``datetime`` pair.

    Both endpoints are converted with :func:`to_microseconds` and then ordered;
    ``end`` before ``start`` is rejected here so the interval constructor never
    sees a reversed pair from an external feed. Returns a pair rather than an
    ``Interval`` so this module keeps no dependency on the interval algebra.
    """
    lower = to_microseconds(start)
    upper = to_microseconds(end)
    if upper < lower:
        raise TimeDomainError(f"interval end {upper} precedes start {lower}")
    return lower, upper


def describe_span(span_us: int) -> str:
    """Deterministic human-readable duration for decision traces.

    Integer division throughout, so the same input always renders the same
    string on every platform and Python build.
    """
    magnitude = as_microseconds(span_us)
    sign = "-" if magnitude < 0 else ""
    remainder = abs(magnitude)
    hours, remainder = divmod(remainder, MICROS_PER_HOUR)
    minutes, remainder = divmod(remainder, MICROS_PER_MINUTE)
    seconds, micros = divmod(remainder, MICROS_PER_SECOND)
    return f"{sign}{hours:02d}:{minutes:02d}:{seconds:02d}.{micros:06d}"
