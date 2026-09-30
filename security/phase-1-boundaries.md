# Phase 1 security and boundary notes

## Asset classification

- Phase 1 scenarios: synthetic internal data.
- Future customer orders: confidential.
- Facility and corridor information: operational-sensitive.
- Vehicle credentials: security-sensitive.
- Audit and incident records: legally sensitive.

## Current boundary controls

- API has no aircraft command path.
- Gateway package is a read-only placeholder and cannot be imported by API.
- Telemetry message types are allowlisted.
- OpenAPI contract tests reject command-like operations.
- Import-linter enforces package separation.
- No production credentials exist.
- No real airspace data is claimed authoritative.

## Threats requiring a future ADR

- spoofed telemetry;
- duplicate or replayed commands;
- cross-tenant data access;
- stale restriction data;
- supply-chain dependency compromise;
- gateway network breakout;
- simulator accidentally pointed at a live endpoint;
- human acceptance token abuse.

## Data handling

Use opaque identifiers and avoid real customer names in fixtures. If medical samples become part of the demo, only a minimal cargo category should exist; recipient identity must not enter planning logs.
