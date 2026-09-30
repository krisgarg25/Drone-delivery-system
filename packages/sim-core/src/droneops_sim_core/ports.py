"""Persistence and observation hooks, as plain protocols.

The persistence agent owns `packages/persistence` and the Alembic migrations; sim-core
must not import it (`.importlinter` keeps that boundary). These protocols are the seam:
a consumer can wire a database writer behind them without the kernel knowing or
caring, and the deterministic run never needs one.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from droneops_sim_core.model import RunMetric, WorldSnapshot


@runtime_checkable
class EventSink(Protocol):
    def on_event(self, event: dict[str, Any]) -> None: ...


@runtime_checkable
class SnapshotSink(Protocol):
    """One `world_snapshot` row per emitted snapshot."""

    def on_snapshot(self, snapshot: WorldSnapshot) -> None: ...


@runtime_checkable
class RunMetricSink(Protocol):
    """One `run_metric` row per metric, carrying formula and denominator."""

    def on_metric(self, metric: RunMetric) -> None: ...


@runtime_checkable
class RunRecordSink(Protocol):
    """The `sim_run` row: identity inputs and terminal outcome."""

    def on_run_complete(self, identity: dict[str, Any], metric_vector: dict[str, Any]) -> None: ...
