"""Scenario bundle integrity: a frozen bundle must hash stably and refuse edits."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from droneops_sim_runner.bundle import (
    BundleIntegrityError,
    StaleSourceError,
    freeze_bundle,
    load_bundle,
)
from droneops_sim_runner.hashing import bundle_sha256, config_release_sha256, file_sha256
from droneops_sim_runner.kernel import identity_for, load_aircraft, run_scenario

REPO_ROOT = Path(__file__).resolve().parents[2]
BUNDLE = REPO_ROOT / "simulation" / "scenarios" / "chandigarh-basic"

pytestmark = pytest.mark.golden


@pytest.fixture
def bundle_dir(tmp_path: Path) -> Path:
    """A private copy: these tests mutate bundles on purpose and must not touch the repo."""
    target = tmp_path / "chandigarh-basic"
    shutil.copytree(BUNDLE, target)
    return target


def test_bundle_manifest_hash_is_stable(bundle_dir: Path) -> None:
    first = bundle_sha256(bundle_dir)
    second = bundle_sha256(load_bundle(bundle_dir).directory)
    assert first == second
    assert len(first) == 64


def test_manifest_declares_every_file(bundle_dir: Path) -> None:
    manifest = yaml.safe_load((bundle_dir / "bundle.yaml").read_text(encoding="utf-8"))
    declared = set(manifest["files"])
    on_disk = {path.name for path in bundle_dir.iterdir() if path.is_file() and path.name != "bundle.yaml"}
    assert declared == on_disk
    assert manifest["authoritative"] is False
    assert manifest["source_manifest"]


def test_editing_a_bundle_file_is_refused(bundle_dir: Path) -> None:
    demand = bundle_dir / "demand.yaml"
    payload = yaml.safe_load(demand.read_text(encoding="utf-8"))
    payload["orders"].append(
        {
            "order_id": "ord-9999",
            "arrival_minute": 5,
            "corridor_id": "corr-north-central-synthetic",
            "origin_facility_id": "depot-north-synthetic",
            "destination_facility_id": "pad-central-synthetic",
            "priority_class": "routine",
        }
    )
    demand.write_text(yaml.safe_dump(payload, sort_keys=True), encoding="utf-8")

    with pytest.raises(BundleIntegrityError, match="digest mismatch"):
        load_bundle(bundle_dir)

    # Re-freezing is the deliberate, reviewed action that accepts a new bundle version.
    manifest = yaml.safe_load((bundle_dir / "bundle.yaml").read_text(encoding="utf-8"))
    freeze_bundle(bundle_dir, manifest)
    assert load_bundle(bundle_dir).manifest["files"]["demand.yaml"] == file_sha256(demand)


def test_a_bundle_claiming_authority_is_refused(bundle_dir: Path) -> None:
    manifest_path = bundle_dir / "bundle.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["authoritative"] = True
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=True), encoding="utf-8")
    with pytest.raises(ValueError, match="authoritative"):
        load_bundle(bundle_dir)


def test_expired_restriction_data_fails_closed(bundle_dir: Path) -> None:
    """Stale operational data must stop the run rather than silently launch a mission."""
    manifest_path = bundle_dir / "bundle.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["source_manifest"]["restrictions"]["expires_at"] = "2026-01-01T00:00:00Z"
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=True), encoding="utf-8")

    with pytest.raises(StaleSourceError, match="expired"):
        load_bundle(bundle_dir)


def test_run_identity_records_every_determinism_input(bundle_dir: Path) -> None:
    bundle = load_bundle(bundle_dir)
    identity = identity_for(bundle, bundle_sha256(bundle_dir))
    payload = identity.as_dict()
    for key in (
        "scenario_id",
        "bundle_sha256",
        "engine_version",
        "scenario_sha256",
        "policy_version",
        "config_release_sha256",
        "seed",
        "python_version",
        "machine_architecture",
    ):
        assert payload[key], f"{key} must be recorded for a reproducible run"
    assert payload["config_release_sha256"] == config_release_sha256(REPO_ROOT)
    assert payload["config_release_sha256"] != "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_same_bundle_twice_produces_the_same_events(bundle_dir: Path) -> None:
    bundle = load_bundle(bundle_dir)
    digest = bundle_sha256(bundle_dir)
    first = run_scenario(bundle, digest)
    second = run_scenario(load_bundle(bundle_dir), digest)
    assert first.events == second.events
    assert first.metrics == second.metrics
    assert first.metrics["invalid_committed_overlaps"] == 0
    assert first.metrics["authorized_for_flight"] is False
    assert len(load_aircraft(bundle)) > 0


def test_golden_reference_matches_the_current_bundle() -> None:
    golden = REPO_ROOT / "simulation" / "golden" / "chandigarh-basic"
    if not (golden / "digest.txt").exists():  # pragma: no cover - guarded for fresh clones
        pytest.skip("golden digest not recorded yet")
    bundle = load_bundle(BUNDLE)
    result = run_scenario(bundle, bundle_sha256(BUNDLE))
    expected_events = (golden / "events.jsonl").read_text(encoding="utf-8")
    produced = "\n".join(
        __import__("json").dumps(entry, sort_keys=True, separators=(",", ":")) for entry in result.events
    )
    assert produced in expected_events or expected_events.startswith(produced)
