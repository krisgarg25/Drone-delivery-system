# Phase 1 security and boundary notes

The full threat model, with the test, import contract or CI job that enforces each
mitigation, is in [`threat-model.md`](threat-model.md). This file is the short boundary
register; where the two disagree, `threat-model.md` is the one backed by executed evidence.

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

These are the seven threats from `technical-build/07-infrastructure-ci-security.md`
section 15 plus the boundary-specific ones. "Enforced today" means a named test, import
contract or CI job refuses the unsafe change; "open" means no such artifact exists yet.

- spoofed telemetry (partly enforced: message-type allowlist, monotonic `sequence`,
  required provenance, telemetry cannot command; **open**: device identity, signing,
  plausibility limits);
- duplicate or replayed commands (enforced: mandatory `Idempotency-Key`, `409` on
  duplicate commit, database overlap rejection; **open**: server-side key store and a
  committed Alembic baseline);
- cross-tenant data access (partly enforced: closed resource-key grammar, API barred from
  the write model, second-tenant concurrency test; **open**: PostgreSQL row-level security
  and log scrubbing);
- stale restriction data (enforced: provenance expiry fails the run closed, frozen bundle
  refuses edits, golden replay must match byte-for-byte);
- supply-chain dependency compromise (enforced: frozen lockfile installs, licence allowlist,
  `pip-audit --strict`, SBOM, pinned actions and hook revisions; **open**: image signing and
  automated dependency updates);
- operational denial through queue flooding (**open — nothing enforces it**): no rate
  limit, no request-size cap, no tenant budget, no safety-event priority lane and no circuit
  breaker exist in the code today. See `threat-model.md` section 7 for the exact state and
  the tests that must land before any queue is introduced;
- gateway network breakout;
- simulator accidentally pointed at a live endpoint (enforced by absence:
  `droneops_sim_core` and `droneops_sim_runner` cannot import `socket`, `httpx`, `requests`,
  `uvicorn`, `sqlalchemy`, `asyncpg` or persistence, and read no environment variable);
- human acceptance token abuse.

A read-only technical boundary is not an operating approval, and the aircraft's onboard
fence and failsafes remain the primary safety layer — see the closing section of
`threat-model.md`.

## Data handling

Use opaque identifiers and avoid real customer names in fixtures. If medical samples become part of the demo, only a minimal cargo category should exist; recipient identity must not enter planning logs.
