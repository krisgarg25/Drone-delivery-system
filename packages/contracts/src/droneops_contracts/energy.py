"""Energy profile and reserve contracts.

Energy quantities are integers (watt-hours): they are energy-critical and use
integer arithmetic, never float, per the conventions. Margins are experiment
parameters read from versioned config, not certified values.
"""

from __future__ import annotations

from pydantic import Field

from .provenance import ContractModel


class EnergyEstimate(ContractModel):
    """The plan ``energy`` block (data model section 8)."""

    profile_id: str = Field(min_length=1)
    estimated_consumption_wh: int = Field(ge=0)
    reserve_required_wh: int = Field(ge=0)
    margin_wh: int = Field(ge=0)


class EnergyReservePolicy(ContractModel):
    """Reserve expressed as fractions of a full battery (illustrative)."""

    policy_version: str
    reserve_fraction: float = Field(ge=0.0, le=1.0)
    alternate_fraction: float = Field(ge=0.0, le=1.0)
    illustrative: bool = True
