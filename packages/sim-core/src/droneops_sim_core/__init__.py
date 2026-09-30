"""Deterministic simulation kernel boundary (Phase 0 queue + Phase 1 world model).

`droneops_sim_core` is pure computation: integer microsecond time, seeded streams and
value objects. Database, network and live adapter imports are forbidden here by
`.importlinter`; persistence integrates through the protocols in `ports.py`.
"""

from __future__ import annotations

from bisect import insort
from collections.abc import Callable
from dataclasses import dataclass, field

from droneops_sim_core.engine import REJECT_REASONS, Engine, EngineResult, STALE_STATE
from droneops_sim_core.model import (
    AIRCRAFT_STATUSES,
    ALTITUDE_REFERENCES,
    PRIORITY_RANK,
    AircraftConfig,
    AircraftProfile,
    BatteryCurvePoint,
    Closure,
    ConfigRelease,
    CorridorEdge,
    Facility,
    Fault,
    GeometryPoint,
    Order,
    RunMetric,
    ScenarioMeta,
    SimRunRecord,
    WeatherTracePoint,
    WorldSnapshot,
    WorldSpec,
)
from droneops_sim_core.ports import EventSink, RunMetricSink, RunRecordSink, SnapshotSink
from droneops_sim_core.streams import STREAM_NAMES, STREAM_OFFSETS, DeterministicStreams


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


__all__ = [
    "AIRCRAFT_STATUSES",
    "ALTITUDE_REFERENCES",
    "AircraftConfig",
    "AircraftProfile",
    "BatteryCurvePoint",
    "Closure",
    "ConfigRelease",
    "CorridorEdge",
    "DeterministicEventQueue",
    "DeterministicStreams",
    "Engine",
    "EngineResult",
    "EventSink",
    "Facility",
    "Fault",
    "GeometryPoint",
    "Order",
    "PRIORITY_RANK",
    "REJECT_REASONS",
    "RunMetric",
    "RunMetricSink",
    "RunRecordSink",
    "STALE_STATE",
    "STREAM_NAMES",
    "STREAM_OFFSETS",
    "ScenarioMeta",
    "ScheduledEvent",
    "SimRunRecord",
    "SnapshotSink",
    "WeatherTracePoint",
    "WorldSnapshot",
    "WorldSpec",
]
