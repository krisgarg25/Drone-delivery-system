from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

MESSAGE_ALLOWLIST: frozenset[str] = frozenset(
    {
        "HEARTBEAT",
        "SYS_STATUS",
        "BATTERY_STATUS",
        "GLOBAL_POSITION_INT",
        "HOME_POSITION",
        "MISSION_CURRENT",
        "STATUSTEXT",
        "RADIO_STATUS",
        "ADS-B_VEHICLE",
    }
)

CommandKind = Literal["NONE_PHASE_1"]


class Observation(BaseModel):
    """Normalized telemetry observation. It cannot create or alter a reservation."""

    aircraft_id: str
    message_type: str
    measurement_time: datetime
    received_time: datetime
    integrity: str = "unknown"

    model_config = ConfigDict(frozen=True)

    def accepted(self) -> bool:
        return self.message_type in MESSAGE_ALLOWLIST
