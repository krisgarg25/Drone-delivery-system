from __future__ import annotations

from bisect import insort
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass(order=True)
class ScheduledEvent:
    at_us: int
    sequence: int
    name: str = field(compare=False)
    handler: Callable[[int], None] = field(compare=False)


class DeterministicEventQueue:
    """Pure event queue using integer microseconds; no wall clock or thread scheduler."""

    def __init__(self) -> None:
        self._events: list[ScheduledEvent] = []
        self._sequence = 0

    def schedule(self, at_us: int, name: str, handler: Callable[[int], None]) -> None:
        if at_us < 0:
            raise ValueError("Simulation time must be non-negative")
        self._sequence += 1
        insort(self._events, ScheduledEvent(at_us, self._sequence, name, handler))

    def advance(self, end_us: int) -> list[str]:
        executed: list[str] = []
        while self._events and self._events[0].at_us <= end_us:
            event = self._events.pop(0)
            event.handler(event.at_us)
            executed.append(event.name)
        return executed

    @property
    def pending_count(self) -> int:
        return len(self._events)
