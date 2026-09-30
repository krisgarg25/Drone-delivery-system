"""Structural no-command check over the OpenAPI contract.

This is the machine-readable form of conventions section 2, boundary #1: no path,
operationId, or schema property in the public contract may expose aircraft
command, upload, parameter, firmware, offboard, launch/takeoff/RTL or
emergency-action semantics. A boundary must not be bypassable by config.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

OPENAPI_PATH = Path(__file__).resolve().parents[2] / "contracts" / "openapi" / "v1.yaml"

# Forbidden substrings matched case-insensitively against paths, operationIds and
# schema property names (never descriptions or enum values).
FORBIDDEN_TERMS = (
    "command",
    "upload",
    "launch",
    "start",
    "land",
    "return_to_base",
    "return-to-base",
    "parameters",
    "firmware",
    "mav_cmd",
    "mavlink",
    "offboard",
    "takeoff",
    "rtl",
    "emergency",
    "action",
    "arm",
)

_HTTP_METHODS = {
    "get",
    "put",
    "post",
    "delete",
    "options",
    "head",
    "patch",
    "trace",
}


def _load_doc() -> dict:
    with OPENAPI_PATH.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _property_names(node: object) -> list[str]:
    names: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "properties" and isinstance(value, dict):
                names.extend(value.keys())
            names.extend(_property_names(value))
    elif isinstance(node, list):
        for item in node:
            names.extend(_property_names(item))
    return names


def _surface_names(doc: dict) -> list[tuple[str, str]]:
    """Return (category, name) pairs that must stay free of command semantics."""

    found: list[tuple[str, str]] = []
    paths = doc.get("paths", {})
    for path, item in paths.items():
        found.append(("path", path))
        if not isinstance(item, dict):
            continue
        for method, op in item.items():
            if method in _HTTP_METHODS and isinstance(op, dict):
                op_id = op.get("operationId")
                if op_id:
                    found.append(("operationId", op_id))
            # Schema property names anywhere inside this path item.
            for prop in _property_names(op):
                found.append(("property", prop))
    schemas = doc.get("components", {}).get("schemas", {})
    for schema_name, schema in schemas.items():
        found.append(("schema", schema_name))
        for prop in _property_names(schema):
            found.append(("property", prop))
    return found


@pytest.mark.parametrize("category,name", _surface_names(_load_doc()))
def test_no_command_surface(category: str, name: str) -> None:
    lowered = name.lower()
    hits = [term for term in FORBIDDEN_TERMS if term in lowered]
    assert not hits, f"{category} {name!r} matches forbidden command term(s): {hits}"


def test_openapi_declares_no_command_rule() -> None:
    doc = _load_doc()
    description = doc["info"]["description"].lower()
    assert "simulation" in description
    assert "not authorised for flight" in description
    assert "no-command" in description or "never contain an aircraft command" in description


def test_idempotency_key_required_on_every_post() -> None:
    doc = _load_doc()
    idem = doc["components"]["parameters"]["IdempotencyKey"]
    assert idem["name"] == "Idempotency-Key"
    assert idem["in"] == "header"
    assert idem["required"] is True

    for path, item in doc["paths"].items():
        post = item.get("post")
        if not post:
            continue
        refs = [p.get("$ref", "") for p in post.get("parameters", [])]
        assert any("#/components/parameters/IdempotencyKey" in r for r in refs), (
            f"POST {path} does not require the Idempotency-Key header"
        )


def test_conflict_returns_409_on_duplicate_commit_posts() -> None:
    doc = _load_doc()
    # The reserve/commit endpoint must offer a 409 duplicate-commit conflict.
    reserve = doc["paths"]["/v1/missions/{id}/reserve"]["post"]
    assert "409" in reserve["responses"]
    conflict = doc["components"]["responses"]["DuplicateCommitConflict"]
    assert conflict["description"]
