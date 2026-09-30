"""Decision record contracts.

Every decision records the configuration-release hash, the ranked eligible
candidates, the rejected candidates with reason codes and the deciding actor.
Rejections are first-class, not discarded. A decision is synthetic and cannot be
constructed without provenance.
"""

from __future__ import annotations

import uuid

from pydantic import Field

from .provenance import (
    ConfigReleaseSha,
    ContractModel,
    Provenance,
    Sha256Hex,
    SyntheticMixin,
    UtcDatetime,
)


class EligibleCandidate(ContractModel):
    plan_version_id: uuid.UUID
    aircraft_id: uuid.UUID
    lateness_s: int = Field(ge=0)
    energy_margin_wh: int
    disturbance_score: float = Field(ge=0.0)
    fairness_score: float = Field(ge=0.0)


class RejectedCandidate(ContractModel):
    plan_version_id: uuid.UUID | None = None
    aircraft_id: uuid.UUID | None = None
    reason_code: str = Field(min_length=1)


class DecisionRecord(ContractModel, SyntheticMixin):
    id: uuid.UUID
    tenant_id: uuid.UUID
    mission_id: uuid.UUID
    input_hash: Sha256Hex
    eligible_candidates: list[EligibleCandidate] = Field(default_factory=list)
    rejected_candidates: list[RejectedCandidate] = Field(default_factory=list)
    selected_plan_id: uuid.UUID | None = None
    reason_codes: list[str] = Field(default_factory=list)
    deciding_actor: str
    created_at: UtcDatetime
    config_release_sha: ConfigReleaseSha
    provenance: Provenance


class DecisionTraceStep(ContractModel):
    """One explainable stage of the planner/checker pipeline."""

    stage: str
    outcome: str
    detail: str
    energy_margin_wh: int | None = None
    timing_uncertainty_s: int | None = None


class DecisionTrace(ContractModel, SyntheticMixin):
    """Console-facing bundle: the decision plus its ordered explanation."""

    mission_id: uuid.UUID
    tenant_id: uuid.UUID
    record: DecisionRecord
    steps: list[DecisionTraceStep] = Field(default_factory=list)
    authoritative: bool = False
