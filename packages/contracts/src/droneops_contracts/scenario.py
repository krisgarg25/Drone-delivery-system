"""Simulation scenario-bundle and reproducibility contracts (sections 13 and 8).

A run is identified by content hashes plus engine/version/seed. ``authoritative``
defaults to false and is part of the frozen bundle schema, so it is hard to
strip. An official scenario must instead record source, retrieval date, licence
and approved use via ``source_manifest``.
"""

from __future__ import annotations

from pydantic import Field

from .geometry import AltitudeReference
from .policy import ProtectedVolumePolicy
from .provenance import (
    ConfigReleaseSha,
    ContractModel,
    JsonValuePayload,
    Sha256Hex,
    SyntheticMixin,
)


class ScenarioUnits(ContractModel):
    altitude_reference: AltitudeReference = AltitudeReference.AGL
    time_zone: str = "Asia/Kolkata"


class ScenarioFault(ContractModel):
    aircraft_ref: str
    kind: str
    at_sim_time_seconds: int = Field(ge=0)


class ScenarioBundle(ContractModel, SyntheticMixin):
    schema_version: int = 1
    scenario_id: str = Field(min_length=1)
    authoritative: bool = False
    units: ScenarioUnits = ScenarioUnits()
    network: JsonValuePayload = Field(default_factory=dict)
    fleet: JsonValuePayload = Field(default_factory=dict)
    demand: JsonValuePayload = Field(default_factory=dict)
    policy: ProtectedVolumePolicy | None = None
    weather_seed: int | None = None
    faults: list[ScenarioFault] = Field(default_factory=list)
    source_manifest: JsonValuePayload = Field(default_factory=dict)


class RunIdentity(ContractModel):
    """Determinism tuple from conventions section 4 / routing section 8."""

    bundle_sha256: Sha256Hex
    engine_version: str
    scenario_sha256: Sha256Hex
    policy_version: str
    config_release_sha256: ConfigReleaseSha
    seed: int
    python_version: str
    machine_architecture: str


class ReplayDiff(ContractModel, SyntheticMixin):
    run_a: RunIdentity
    run_b: RunIdentity
    event_hash_equal: bool
    metrics_equal: bool
    differing_metrics: list[str] = Field(default_factory=list)


class ScenarioComparison(ContractModel, SyntheticMixin):
    run_a: RunIdentity
    run_b: RunIdentity
    compared_metrics: dict[str, str] = Field(default_factory=dict)
    verdict: str
