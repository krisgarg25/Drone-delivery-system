"""Domain event envelope and the event catalogue (data model section 10).

The unique key ``(stream_type, stream_id, seq)`` provides optimistic stream
ordering. Every event carries its own occurrence time and causal chain, so late
data never mutates an earlier event (invariant I10).
"""

from __future__ import annotations

import uuid
from enum import Enum
from typing import Literal

from pydantic import Field

from .provenance import (
    ConfigReleaseSha,
    ContractModel,
    JsonValuePayload,
    UtcDatetime,
)

# The catalogue as written in the spec. Note: the spec lists 23 event types, not
# the "22" referenced in some summaries; all listed events are modelled here.
EVENT_TYPES: tuple[str, ...] = (
    "order.registered",
    "order.cancelled",
    "mission.created",
    "mission.planned",
    "mission.replanned",
    "reservation.committed",
    "reservation.activated",
    "reservation.completed",
    "reservation.cancelled",
    "reservation.unresolved",
    "telemetry.observed",
    "telemetry.stale",
    "conflict.detected",
    "checker.verdictRecorded",
    "aircraft.stateChanged",
    "priority.requested",
    "proposal.previewed",
    "proposal.accepted",
    "proposal.declined",
    "restriction.updated",
    "facility.outage",
    "usage.mission_charged",
    "config.released",
)


def _member_name(value: str) -> str:
    return value.replace(".", "_").upper()


EventType = Enum(
    "EventType",
    [(_member_name(v), v) for v in EVENT_TYPES],
    type=str,
)  # type: ignore[assignment]


class EventStreamType(str, Enum):
    ORDER = "order"
    MISSION = "mission"
    AIRCRAFT = "aircraft"
    FACILITY = "facility"
    RESERVATION = "reservation"
    CONFIG = "config"
    SYSTEM = "system"


class EventEnvelope(ContractModel):
    schema_version: Literal[1] = 1
    event_id: uuid.UUID
    event_type: EventType
    occurred_at: UtcDatetime
    tenant_id: uuid.UUID
    stream_type: EventStreamType
    stream_id: uuid.UUID
    seq: int = Field(ge=0)
    correlation_id: str
    causation_id: str | None = None
    actor: str
    config_release_sha: ConfigReleaseSha
    payload: JsonValuePayload = Field(default_factory=dict)
