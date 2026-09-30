"""Resource, routing and access policy contracts.

Resource keys are validated against the closed set of allowed kinds, and the
versioned protected-volume policy from ``11-simulation-routing-engine.md``
section 5 is modelled here. Every margin is marked illustrative: the safety
requirement is the invariant and test suite, not a specific number.
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated

from pydantic import AfterValidator, Field

from .geometry import AltitudeReference
from .provenance import ContractModel


class ResourceKind(str, Enum):
    CORRIDOR = "corridor"
    VOLUME_CELL = "volume_cell"
    CROSSING = "crossing"
    PAD = "pad"
    CHARGER_BAY = "charger_bay"
    CREW = "crew"


ALLOWED_RESOURCE_KINDS: frozenset[str] = frozenset(k.value for k in ResourceKind)


def validate_resource_key(value: str) -> str:
    """A resource key is ``<kind>:<qualifier>...`` with an allowed kind."""

    if not value or ":" not in value:
        raise ValueError("resource key must be of the form 'kind:qualifier...'")
    kind, rest = value.split(":", 1)
    if kind not in ALLOWED_RESOURCE_KINDS:
        raise ValueError(
            f"resource kind {kind!r} is not one of {sorted(ALLOWED_RESOURCE_KINDS)}"
        )
    if not rest:
        raise ValueError("resource key qualifier must be non-empty")
    return value


ResourceKey = Annotated[str, AfterValidator(validate_resource_key)]


class ProtectedVolumePolicy(ContractModel):
    """Versioned protected-volume margins (illustrative experiment parameters)."""

    schema_version: int = 1
    policy_version: str = "sim-policy-v1"
    altitude_reference: AltitudeReference = AltitudeReference.AGL
    horizontal_margin_m: int = Field(ge=0)
    vertical_margin_m: int = Field(ge=0)
    position_uncertainty_m: int = Field(ge=0)
    timing_uncertainty_s: int = Field(ge=0)
    transition_buffer_s: int = Field(ge=0)
    illustrative: bool = True


class OperatorRole(str, Enum):
    """Allowed Phase 1 roles. There is deliberately no ``aircraft_commander``."""

    PARTNER_ORDER_WRITER = "partner_order_writer"
    SCHEDULER_OPERATOR = "scheduler_operator"
    PILOT_OBSERVER = "pilot_observer"
    SAFETY_REVIEWER = "safety_reviewer"
    AUDITOR = "auditor"
    PLATFORM_ADMIN = "platform_admin"
