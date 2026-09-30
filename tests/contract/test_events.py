"""Event-catalogue contract tests (data model section 10)."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from droneops_contracts import EventEnvelope, EventType
from droneops_contracts.events import EVENT_TYPES

EVENTS_DIR = Path(__file__).resolve().parents[2] / "contracts" / "events"

ENVELOPE_KEYS = {
    "schema_version",
    "event_id",
    "event_type",
    "occurred_at",
    "tenant_id",
    "stream_type",
    "stream_id",
    "seq",
    "correlation_id",
    "causation_id",
    "actor",
    "config_release_sha",
    "payload",
}


def test_catalogue_has_23_events() -> None:
    # The spec section 10 lists 23 events (summaries that say "22" are stale).
    assert len(EVENT_TYPES) == 23
    assert len({EventType[v.replace(".", "_").upper()] for v in EVENT_TYPES}) == 23


def test_one_file_per_event() -> None:
    on_disk = {p.stem for p in EVENTS_DIR.glob("*.yaml")}
    assert set(EVENT_TYPES) == on_disk


@pytest.mark.parametrize("name", sorted(EVENT_TYPES))
def test_each_event_file_is_a_valid_envelope(name: str) -> None:
    data = yaml.safe_load((EVENTS_DIR / f"{name}.yaml").read_text(encoding="utf-8"))
    assert set(data) == ENVELOPE_KEYS
    env = EventEnvelope(**data)
    assert env.event_type.value == name


def test_envelope_rejects_unknown_event_type() -> None:
    base = yaml.safe_load(
        (EVENTS_DIR / "reservation.committed.yaml").read_text(encoding="utf-8")
    )
    base["event_type"] = "reservation.exploded"
    with pytest.raises(ValidationError):
        EventEnvelope(**base)


def test_stream_ordering_key_is_present() -> None:
    data = yaml.safe_load(
        (EVENTS_DIR / "mission.created.yaml").read_text(encoding="utf-8")
    )
    env = EventEnvelope(**data)
    # (stream_type, stream_id, seq) is the optimistic-ordering key.
    assert env.stream_type is not None
    assert isinstance(env.stream_id, uuid.UUID)
    assert env.seq >= 0
