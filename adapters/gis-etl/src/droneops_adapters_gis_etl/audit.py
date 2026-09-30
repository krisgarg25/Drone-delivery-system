"""The audit: re-read what was written and try to make it lie.

The generation code already refuses to emit an unflagged obstacle. That is not enough:
a writer and its own assertion can share a bug, a hand-edit can strip a field, and a
future refactor can move provenance somewhere the checker does not look. So the audit
reads the artefacts *back from disk* - the same bytes a consumer would read - and
checks them independently.

It is the module behind ``cli audit`` and behind the lint test
``test_generated_obstacles_carry_every_synthetic_flag``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .config import GisConfig
from .corridor import audit_corridor
from .errors import GisEtlError
from .geom import read_jsonl
from .manifest import load_manifest
from .provenance import assert_synthetic_flags
from .vertical import audit_feature, assert_no_safety_implication

#: Phrases that would read as an operational clearance claim if they ever appear in
#: output without being negated. The negations the honest notes use are whitelisted.
CLAIM_PATTERN = re.compile(
    r"\b(safe route|safest corridor|clearance (is |has been )?(verified|ensured|guaranteed)|"
    r"obstacle[- ]free|no obstacles (here|in this)|airspace is clear|authori[sz]ed for flight|"
    r"operationally approved|risk[- ]free)\b",
    re.IGNORECASE,
)
NEGATIONS = ("not", "never", "no ", "without", "cannot", "void", "non-")


def audit_snapshot(config: GisConfig) -> dict[str, Any]:
    """Audit every artefact in the processed dataset directory."""
    processed = config.processed_dir
    if not processed.is_dir():
        raise GisEtlError(f"no processed dataset at {processed}; run the pipeline first")

    obstacles_path = processed / "obstacles" / "obstacles.jsonl"
    corridors_path = processed / "corridors" / "candidate_corridors.geojson"
    manifest_path = processed / "manifest.yaml"

    result: dict[str, Any] = {
        "obstacles": _audit_obstacles(obstacles_path),
        "corridors": _audit_corridors(corridors_path),
        "layers": _audit_layers(processed / "layers"),
        "manifest": _audit_manifest(manifest_path),
        "text_scan": _scan_for_claims(processed),
    }
    problems = [
        problem
        for section in result.values()
        if isinstance(section, dict)
        for problem in section.get("problems", [])
    ]
    result["ok"] = not problems
    result["problem_count"] = len(problems)
    if problems:
        raise GisEtlError(
            "snapshot audit failed:\n" + "\n".join(f"  - {problem}" for problem in problems[:20])
        )
    return result


def _audit_obstacles(path: Path) -> dict[str, Any]:
    problems: list[str] = []
    if not path.is_file():
        return {"present": False, "problems": [f"missing obstacle file {path}"]}
    records = read_jsonl(path)
    kinds: dict[str, int] = {}
    bases: dict[str, int] = {}
    for index, record in enumerate(records):
        properties = record.get("properties") or {}
        identifier = str(properties.get("obstacle_id", f"line {index}"))
        try:
            assert_synthetic_flags(properties, identifier)
        except GisEtlError as exc:
            problems.append(str(exc))
            continue
        try:
            audit_feature(record)
        except GisEtlError as exc:
            problems.append(str(exc))
        kinds[str(properties.get("obstacle_kind"))] = kinds.get(str(properties.get("obstacle_kind")), 0) + 1
        bases[str(properties.get("height_basis"))] = bases.get(str(properties.get("height_basis")), 0) + 1
        for field in ("assumed_height_m", "altitude_reference", "uncertainty_margin_m"):
            if field not in properties:
                problems.append(f"obstacle {identifier} lacks {field!r}")
    return {
        "present": True,
        "count": len(records),
        "fully_flagged": len(records) - len(problems),
        "by_kind": dict(sorted(kinds.items())),
        "by_height_basis": dict(sorted(bases.items())),
        "problems": problems[:40],
    }


def _audit_corridors(path: Path) -> dict[str, Any]:
    problems: list[str] = []
    if not path.is_file():
        return {"present": False, "problems": []}
    collection = json.loads(path.read_text(encoding="utf-8"))
    features = collection.get("features", [])
    for index, feature in enumerate(features):
        properties = feature.get("properties") or {}
        identifier = str(properties.get("corridor_id", f"index {index}"))
        for key, expected in (
            ("safety_claim", False),
            ("planning_heuristic_only", True),
            ("requires_survey", True),
            ("authoritative", False),
            ("synthetic", True),
        ):
            if properties.get(key) is not expected:
                problems.append(f"corridor {identifier} has {key}={properties.get(key)!r}, expected {expected!r}")
        try:
            audit_corridor(feature)
        except GisEtlError as exc:
            problems.append(str(exc))
        try:
            assert_no_safety_implication(properties, identifier)
        except GisEtlError as exc:
            problems.append(str(exc))
    if collection.get("safety_claim") is not False:
        problems.append("corridor collection does not deny a safety claim")
    return {"present": True, "count": len(features), "problems": problems[:40]}


def _audit_layers(layers_dir: Path) -> dict[str, Any]:
    problems: list[str] = []
    counted: dict[str, int] = {}
    for path in sorted(layers_dir.glob("*.jsonl")):
        count = 0
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            count += 1
            properties = record.get("properties") or {}
            provenance = properties.get("provenance") or {}
            for key in ("source", "retrieved_at", "response_sha256", "query_sha256"):
                if not provenance.get(key):
                    problems.append(f"{path.name}: feature lacks provenance.{key}")
                    break
        counted[path.name] = count
    for path in sorted(layers_dir.glob("*.geojson")):
        collection = json.loads(path.read_text(encoding="utf-8"))
        if collection.get("authoritative") is not False:
            problems.append(f"{path.name}: layer collection does not declare authoritative=false")
        counted[path.name] = len(collection.get("features", []))
    return {"counts": counted, "problems": problems[:40]}


def _audit_manifest(path: Path) -> dict[str, Any]:
    problems: list[str] = []
    if not path.is_file():
        return {"present": False, "problems": [f"missing manifest {path}"]}
    document = load_manifest(path)
    for key in ("schema_version", "dataset_id", "retrieved_at", "boundary", "corridor", "terrain", "obstacles"):
        if key not in document:
            problems.append(f"manifest missing {key}")
    if document.get("authoritative") is not False:
        problems.append("manifest is not marked authoritative=false")
    boundary = document.get("boundary", {})
    if boundary.get("relation_id") != 1942809:
        problems.append("manifest boundary relation is not 1942809")
    if boundary.get("licence") != "ODbL":
        problems.append("manifest boundary licence is not ODbL")
    if not str(boundary.get("hash", "")).startswith("sha256:"):
        problems.append("manifest boundary hash is not a sha256")
    if document.get("corridor", {}).get("safety_claim") is not False:
        problems.append("manifest corridor claims safety")
    obstacles = document.get("obstacles", {})
    if obstacles.get("requires_survey") is not True:
        problems.append("manifest obstacles do not require survey")
    if "tagged height" not in str(obstacles.get("building_height_coverage", "")):
        problems.append("manifest lacks the measured building height coverage string")
    if document.get("terrain", {}).get("resolution_m") != 30:
        problems.append("manifest terrain resolution is not the declared 30 m")
    if "not authorised for flight" not in json.dumps(document).lower():
        problems.append("manifest does not state that it is not authorised for flight")
    return {
        "present": True,
        "dataset_id": document.get("dataset_id"),
        "coverage_string": obstacles.get("building_height_coverage"),
        "problems": problems,
    }


def _scan_for_claims(processed: Path) -> dict[str, Any]:
    """Every artefact text, searched for a phrase that asserts safety without denying it."""
    hits: list[str] = []
    for path in sorted(processed.rglob("*")):
        if not path.is_file() or path.suffix not in {".json", ".jsonl", ".geojson", ".yaml"}:
            continue
        text = path.read_text(encoding="utf-8")
        for match in CLAIM_PATTERN.finditer(text):
            window = text[max(0, match.start() - 80) : match.end() + 80].lower()
            if any(negation in window for negation in NEGATIONS):
                continue
            hits.append(f"{path.name}: {match.group(0)!r} asserted without negation")
    return {"files_scanned": True, "problems": hits[:20]}
