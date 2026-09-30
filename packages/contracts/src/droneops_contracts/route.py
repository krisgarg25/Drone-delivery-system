"""Route, 4D plan profile and plan-payload contracts (data model section 8)."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, StringConstraints

from .energy import EnergyEstimate
from .geometry import Position
from .policy import ProtectedVolumePolicy, ResourceKey
from .provenance import ConfigReleaseSha, ContractModel

Crs4326 = Annotated[str, StringConstraints(pattern=r"^EPSG:4326$")]


class RoutePoint(ContractModel):
    """A single time-indexed waypoint. ``t`` is integer seconds from plan start."""

    t: int = Field(ge=0)
    position: Position
    speed_ms: float = Field(ge=0.0)


class CoordinateTimeProfile(ContractModel):
    type: Literal["coordinate-time-profile"] = "coordinate-time-profile"
    crs: Crs4326 = "EPSG:4326"
    points: list[RoutePoint] = Field(min_length=1)


class PlanPayload(ContractModel):
    """The full plan-version payload; margins are exposed, not collapsed."""

    plan_version: str
    route: CoordinateTimeProfile
    protected_volume: ProtectedVolumePolicy
    resources: list[ResourceKey] = Field(min_length=1)
    energy: EnergyEstimate
    config_release_sha: ConfigReleaseSha
