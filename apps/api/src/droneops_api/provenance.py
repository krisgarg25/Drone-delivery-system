"""Provenance vocabulary shared by every Phase 4 response.

Conventions section 14 and ``technical-build/04`` section 6 make one demand of the
control plane: a reader must never confuse a proposal with a commitment, a commitment
with an observation, or either of them with synthetic data. Four fields answer that,
and they are mandatory rather than advisory:

``phase``
    Which build produced this (``phase-4-simulation``).
``source_kind``
    ``planned`` (a planner proposal, nothing more), ``committed`` (the reservation
    ledger's own record - the only authority in the system), ``observed`` (a telemetry
    measurement, simulated in this build) or ``synthetic`` (a fixture, run identity or
    comparison produced for the simulation).
``authoritative``
    True only for ``committed``. A proposal is never authoritative, and a simulated
    observation never becomes one by being displayed.
``simulation_notice``
    The standing statement: Simulation only. Not authorised for flight.

The invariant is enforced in one place so a router cannot opt out of it.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, model_validator

#: The build that produced this surface. Reported on every response.
SIMULATION_PHASE = "phase-4-simulation"

#: Conventions section 14: every screen, report and export carries this sentence.
SIMULATION_NOTICE = "Simulation only. Not authorised for flight."

#: There is no flight authority in this repository, in any phase, at any flag value.
NOT_AUTHORISED_FOR_FLIGHT = False


class SourceKind(StrEnum):
    """How the reader should treat the data, exactly as ``technical-build/04`` names it."""

    PLANNED = "planned"
    COMMITTED = "committed"
    OBSERVED = "observed"
    SYNTHETIC = "synthetic"


class ProvenanceModel(BaseModel):
    """Base for every response model: provenance fields are required, not optional.

    ``authoritative`` is derived, not chosen: only a committed ledger record may claim
    it, so a stub that returns a proposal cannot accidentally advertise authority.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    phase: str = SIMULATION_PHASE
    source_kind: SourceKind
    authoritative: bool = False
    simulation_notice: str = SIMULATION_NOTICE

    @model_validator(mode="after")
    def _only_committed_is_authoritative(self) -> ProvenanceModel:
        if self.authoritative and self.source_kind is not SourceKind.COMMITTED:
            raise ValueError("only a committed ledger record may be authoritative")
        if self.source_kind is SourceKind.COMMITTED and not self.authoritative:
            raise ValueError("a committed ledger record must be authoritative")
        return self


def provenance_fields(kind: SourceKind) -> dict[str, Any]:
    """Keyword arguments that stamp ``kind`` onto a response model.

    Keeps routers free of provenance bookkeeping: they name the kind, this supplies the
    phase, the derived authority and the standing statement.
    """

    return {
        "phase": SIMULATION_PHASE,
        "source_kind": kind,
        "authoritative": kind is SourceKind.COMMITTED,
        "simulation_notice": SIMULATION_NOTICE,
    }
