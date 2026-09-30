"""Frozen scenario bundle loading.

A bundle is a directory of plain-text inputs with a `bundle.yaml` manifest that records
the sha256 of every file. The runner refuses to execute a bundle whose files no longer
match its manifest: a silently edited scenario is not a reproducible experiment.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from droneops_sim_runner.hashing import bundle_sha256, file_sha256

MANIFEST_NAME = "bundle.yaml"
REQUIRED_TOP_LEVEL = (
    "schema_version",
    "scenario_id",
    "authoritative",
    "seed",
    "runtime",
    "files",
    "source_manifest",
)


@dataclass(frozen=True)
class ScenarioBundle:
    directory: Path
    manifest: dict[str, Any]
    documents: dict[str, Any]

    @property
    def scenario_id(self) -> str:
        return str(self.manifest["scenario_id"])

    @property
    def seed(self) -> int:
        return int(self.manifest["seed"])

    @property
    def policy_version(self) -> str:
        runtime = self.manifest["runtime"]
        return str(runtime.get("policy_version", "unset"))

    @property
    def bundle_sha256(self) -> str:
        return bundle_sha256(self.directory)

    def document(self, name: str) -> dict[str, Any]:
        value = self.documents[name]
        if not isinstance(value, dict):
            raise ValueError(f"bundle document {name} must be a mapping")
        return value


class BundleIntegrityError(RuntimeError):
    """Raised when a bundle file does not match its frozen manifest."""


class StaleSourceError(RuntimeError):
    """Raised when an input's provenance has expired. Expired data fails closed."""


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def assert_sources_fresh(bundle: ScenarioBundle) -> None:
    """An expired restriction or weather snapshot cannot launch anything, even simulated.

    Fail-closed is the only acceptable reading of stale operational data
    (07-infrastructure-ci-security.md section 15).
    """
    documents = bundle.manifest["files"]
    if "policies.yaml" not in documents:
        raise ValueError("bundle must declare a policies.yaml epoch")
    epoch = _parse_timestamp(str(bundle.document("policies.yaml")["epoch"])).replace(tzinfo=UTC)

    sources = bundle.manifest["source_manifest"]
    for name in sorted(sources):
        entry = sources[name]
        if not isinstance(entry, dict) or "expires_at" not in entry:
            continue
        expires = _parse_timestamp(str(entry["expires_at"])).replace(tzinfo=UTC)
        if expires <= epoch:
            raise StaleSourceError(
                f"{bundle.scenario_id}: {name} expired at {entry['expires_at']} before the scenario "
                f"epoch {epoch.isoformat()}. Refresh and re-freeze the bundle."
            )


def _load_yaml(path: Path) -> dict[str, Any]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError(f"{path} must contain a mapping")
    return loaded


def load_bundle(directory: Path) -> ScenarioBundle:
    manifest_path = directory / MANIFEST_NAME
    if not manifest_path.exists():
        raise FileNotFoundError(f"{directory} has no {MANIFEST_NAME}; run `droneops-sim freeze`")

    manifest = _load_yaml(manifest_path)
    missing = [key for key in REQUIRED_TOP_LEVEL if key not in manifest]
    if missing:
        raise ValueError(f"{manifest_path} is missing required keys: {', '.join(missing)}")

    # Synthetic honesty: a bundle that claims to be authoritative is not a simulator input.
    if manifest["authoritative"] is not False:
        raise ValueError(f"{manifest['scenario_id']} must declare authoritative: false")

    files = manifest["files"]
    documents: dict[str, Any] = {}
    for name, recorded in sorted(files.items()):
        path = directory / str(name)
        if not path.exists():
            raise BundleIntegrityError(f"{manifest['scenario_id']}: declared file {name} is absent")
        actual = file_sha256(path)
        if actual != recorded:
            raise BundleIntegrityError(
                f"{manifest['scenario_id']}: {name} digest mismatch. "
                f"manifest={recorded} actual={actual}. Re-freeze the bundle deliberately."
            )
        documents[str(name)] = _load_yaml(path)

    if not manifest["source_manifest"]:
        raise ValueError("bundle must record a source_manifest with provenance")

    bundle = ScenarioBundle(directory=directory, manifest=manifest, documents=documents)
    assert_sources_fresh(bundle)
    return bundle


def freeze_bundle(directory: Path, manifest: dict[str, Any]) -> str:
    """Write (or rewrite) the file digest section of a bundle manifest."""
    files = {
        path.name: file_sha256(path)
        for path in sorted(directory.iterdir())
        if path.is_file() and path.name != MANIFEST_NAME
    }
    manifest = {**manifest, "files": files}
    (directory / MANIFEST_NAME).write_text(
        yaml.safe_dump(manifest, sort_keys=True, allow_unicode=True),
        encoding="utf-8",
    )
    return bundle_sha256(directory)
