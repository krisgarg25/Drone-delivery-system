"""OpenAPI diff and no-command-route gate.

Two independent jobs:

1. **Diff.** `contracts/openapi/v1.yaml` is the announced surface and is reviewed by
   hand. The app may only expose routes that the contract already declares, so an
   undocumented endpoint fails CI. Routes the contract declares but the app has not
   implemented yet are reported as planned work, never as a failure.
2. **No-command check.** Neither side may contain a command, takeoff, land, RTL, upload,
   parameter, firmware, offboard or arm operation. This is the structural guarantee that
   the API cannot become a flight controller, and it does not depend on a feature flag.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.check_boundaries import FORBIDDEN_ROUTE_TOKENS

CONTRACT_PATH = Path("contracts/openapi/v1.yaml")
HTTP_METHODS = ("get", "put", "post", "patch", "delete")

# Operational surface is always exposed and is not part of the reviewed business contract.
OPERATIONAL_ROUTES = {"/health", "/version", "/openapi.json", "/docs", "/redoc", "/docs/oauth2-redirect"}

# Phase 0 endpoints kept while the Phase 4 order surface lands. Each entry is reviewed,
# command-free, and must be deleted from this list when the contract declares its
# replacement (`/v1/orders`). Adding a name here is a contract change, not a fix.
LEGACY_PHASE0_ROUTES = {"/v1/simulated-orders"}


def route_pairs(spec: dict[str, Any]) -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    for path, operations in (spec.get("paths") or {}).items():
        if not isinstance(operations, dict):
            continue
        for method in HTTP_METHODS:
            if method in operations:
                pairs.add((method.upper(), str(path)))
    return pairs


def operation_tokens(spec: dict[str, Any]) -> list[str]:
    tokens: list[str] = []
    for path, operations in (spec.get("paths") or {}).items():
        if not isinstance(operations, dict):
            continue
        for method, operation in operations.items():
            operation_id = operation.get("operationId", "") if isinstance(operation, dict) else ""
            tokens.append(f"{method}/{path}/{operation_id}")
    return tokens


def command_violations(tokens: list[str], origin: str) -> list[str]:
    hits: list[str] = []
    for token in tokens:
        lowered = token.lower()
        for forbidden in FORBIDDEN_ROUTE_TOKENS:
            if re.search(rf"[/_{{-]?{forbidden}[/_}}-]?", lowered):
                hits.append(f"{origin}: '{token}' matches forbidden command token '{forbidden}'")
    return hits


def app_spec() -> dict[str, Any]:
    from fastapi.testclient import TestClient

    from droneops_api.main import app

    response = TestClient(app).get("/openapi.json")
    response.raise_for_status()
    spec: dict[str, Any] = response.json()
    return spec


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, help="write the diff as JSON")
    args = parser.parse_args(argv)

    if not CONTRACT_PATH.exists():
        print(f"missing {CONTRACT_PATH}", file=sys.stderr)
        return 1

    contract: dict[str, Any] = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8")) or {}
    contract_paths = route_pairs(contract)
    app_paths = route_pairs(app_spec())
    exempt = OPERATIONAL_ROUTES | LEGACY_PHASE0_ROUTES

    problems: list[str] = []
    problems += command_violations(operation_tokens(contract), "contract")
    problems += command_violations(operation_tokens(app_spec()), "app")
    if not contract_paths:
        problems.append(f"{CONTRACT_PATH} declares no paths")
    for method, path in sorted(app_paths - contract_paths):
        if path in exempt:
            continue
        problems.append(f"app exposes undeclared route {method} {path}")

    summary = {
        "contract_routes": len(contract_paths),
        "implemented_routes": len(app_paths & contract_paths),
        "planned_routes": len(contract_paths - app_paths),
        "exposed_exempt_routes": len([path for _, path in app_paths if path in exempt]),
        "undeclared_routes": len([1 for _, path in app_paths - contract_paths if path not in exempt]),
        "violations": problems,
    }
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")

    print(
        f"contract routes: {summary['contract_routes']}, implemented: {summary['implemented_routes']}, "
        f"planned: {summary['planned_routes']}, undeclared: {summary['undeclared_routes']}"
    )
    if problems:
        print("OPENAPI GATE FAILED:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print("no command route, no undeclared route")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
