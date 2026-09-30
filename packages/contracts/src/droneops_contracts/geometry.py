"""Geometry primitives: altitude reference, positions and time windows.

Invariant I9 (explicit altitude reference) is enforced here: every altitude is
carried together with an :class:`AltitudeReference`, and a position record whose
reference is ``UNSPECIFIED`` cannot be constructed.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import Field, model_validator

from .provenance import ContractModel


class AltitudeReference(str, Enum):
    AGL = "AGL"
    AMSL = "AMSL"
    GROUND = "GROUND"
    PAD = "PAD"
    UNSPECIFIED = "UNSPECIFIED"


class TimeWindow(ContractModel):
    """A UTC window. Retained public name and shape from Phase 0."""

    start_utc: datetime
    end_utc: datetime
    version: int = 1

    @model_validator(mode="after")
    def _check_ordering(self) -> TimeWindow:
        from .provenance import ensure_utc

        ensure_utc(self.start_utc)
        ensure_utc(self.end_utc)
        if self.end_utc <= self.start_utc:
            raise ValueError("time window end must be after start")
        return self


class Position(ContractModel):
    """A geodetic point whose altitude carries an explicit reference (I9)."""

    lat: float = Field(ge=-90.0, le=90.0)
    lon: float = Field(ge=-180.0, le=180.0)
    altitude_m: float
    altitude_reference: AltitudeReference
    uncertainty_m: float | None = Field(default=None, ge=0.0)

    @model_validator(mode="after")
    def _reject_unspecified(self) -> Position:
        if self.altitude_reference is AltitudeReference.UNSPECIFIED:
            raise ValueError(
                "altitude_reference must not be UNSPECIFIED on a position record (I9)"
            )
        return self
