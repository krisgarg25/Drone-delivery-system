from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from droneops_contracts import MissionStatus, PriorityClass

ALLOWED_TRANSITIONS = {
    MissionStatus.DRAFT: {MissionStatus.ELIGIBLE, MissionStatus.CANCELLED},
    MissionStatus.ELIGIBLE: {MissionStatus.PLANNING, MissionStatus.CANCELLED},
    MissionStatus.PLANNING: {
        MissionStatus.RESERVED_PENDING_COMMIT,
        MissionStatus.FAILED,
        MissionStatus.CANCELLED,
    },
    MissionStatus.RESERVED_PENDING_COMMIT: {
        MissionStatus.ACCEPTED,
        MissionStatus.FAILED,
        MissionStatus.CANCELLED,
    },
    MissionStatus.ACCEPTED: {MissionStatus.ACTIVATED, MissionStatus.CANCELLED},
    # ACTIVATED is a simulated mission state in Phase 1 and is not aircraft authority.
    MissionStatus.ACTIVATED: {MissionStatus.IN_PROGRESS, MissionStatus.UNRESOLVED},
    MissionStatus.IN_PROGRESS: {MissionStatus.COMPLETE, MissionStatus.UNRESOLVED},
    MissionStatus.COMPLETE: set(),
    MissionStatus.CANCELLED: set(),
    MissionStatus.FAILED: set(),
    MissionStatus.UNRESOLVED: {MissionStatus.FAILED, MissionStatus.CANCELLED},
}


@dataclass
class Mission:
    id: uuid.UUID
    order_id: uuid.UUID
    origin_facility_id: str
    destination_facility_id: str
    priority: PriorityClass
    status: MissionStatus = MissionStatus.DRAFT
    created_at: datetime | None = None
    updated_at: datetime | None = None
    status_history: list[MissionStatus] = field(default_factory=list)


def create_simulated_mission(
    mission_id: uuid.UUID,
    order_id: uuid.UUID,
    origin_facility_id: str,
    destination_facility_id: str,
    priority: PriorityClass,
    at: datetime,
) -> Mission:
    return Mission(
        id=mission_id,
        order_id=order_id,
        origin_facility_id=origin_facility_id,
        destination_facility_id=destination_facility_id,
        priority=priority,
        status=MissionStatus.DRAFT,
        created_at=at,
        updated_at=at,
        status_history=[MissionStatus.DRAFT],
    )


def transition_mission(mission: Mission, to: MissionStatus) -> Mission:
    if to not in ALLOWED_TRANSITIONS[mission.status]:
        raise ValueError(f"Invalid mission transition: {mission.status} -> {to}")
    mission.status = to
    mission.status_history.append(to)
    return mission
