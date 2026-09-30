from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timezone, UTC

UTC = UTC


class Clock(ABC):
    @abstractmethod
    def now(self) -> datetime:
        raise NotImplementedError


class FrozenClock(Clock):
    """Clock used by Phase 1 API and deterministic tests. Production telemetry must never use it."""

    def __init__(self, fixed_time: datetime) -> None:
        if fixed_time.tzinfo is None:
            raise ValueError("Clock times must be timezone-aware")
        self._now = fixed_time.astimezone(UTC)

    def now(self) -> datetime:
        return self._now


# The Phase 0 API deliberately has a fixed clock so operational records do not imply live activity.
_CLOCK: Clock = FrozenClock(datetime(2026, 9, 30, 12, 0, 0, tzinfo=UTC))


def set_clock(clock: Clock) -> None:
    global _CLOCK
    _CLOCK = clock


def get_clock() -> Clock:
    return _CLOCK
