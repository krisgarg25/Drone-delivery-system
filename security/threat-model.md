# DroneOps threat model (Phase 0 / Phase 1)

Scope: the seven threats in `technical-build/07-infrastructure-ci-security.md` section 15.
Status of this document: **technical boundary model**, not an operating approval. See
"Read this first" and "What this model does not cover" below.

Owner: `@krisgarg25` (`.github/CODEOWNERS`). Review cadence: every change to
`.importlinter`, `scripts/check_boundaries.py`, `contracts/openapi/v1.yaml` or
`packages/telemetry/`.

## Read this first

Every mitigation below is a *software* control that makes an unsafe build fail loudly. It
is not evidence that the system is safe to fly, and nothing in this repository has been
approved by any authority. The controls are enforced by three mechanisms only:

1. `import-linter` contracts in `.importlinter` (18 contracts, verified `18 kept, 0 broken`
   on 84 files / 263 dependencies);
2. tests under `tests/`, `packages/*/tests/`;
3. jobs in `.github/workflows/ci.yml`.

Anything not named by a real file path below is **not** enforced and is listed as a gap.

---

## 1. Spoofed telemetry

**Threat.** An attacker, or a misbehaving simulator, injects a position/attitude reading
that the platform treats as truth, moving an aircraft's believed location.

**Design response.** Telemetry is a projection, never a decision-maker. It cannot plan,
dispatch or commit, so a forged reading cannot release or move a reservation by itself.

**Enforced by (verified to exist):**

| Control | Where | Proof it bites |
|---|---|---|
| Message-type allowlist (`MESSAGE_ALLOWLIST`: `HEARTBEAT` and friends); unlisted frames are dropped, not degraded | `packages/telemetry/src/droneops_telemetry/__init__.py` (`Observation.accepted()`) | `tests/test_safety_boundaries.py::test_unlisted_telemetry_is_not_accepted` (a `MAV_CMD` frame is rejected) |
| `sequence` is a required, monotonic-with-time field; out-of-order or clock-inconsistent observations are rejected at the model boundary | `packages/contracts/src/droneops_contracts/telemetry.py` (`sequence: int = Field(ge=0)`, `_clock_ordering`) | `tests/contract/test_events.py::test_stream_ordering_key_is_present`, `tests/contract/test_invariants.py::test_naive_datetime_rejected`, `::test_non_utc_datetime_rejected` |
| Provenance is a required field: no observation exists without a source and a validity window | `packages/contracts/src/droneops_contracts/provenance.py` (`Provenance`, `SyntheticMixin`) | `tests/contract/test_schema_sync.py::test_schema_matches_model` |
| Uncertainty is explicit and must retain its volume; integrity status is a closed enum | `telemetry.py` (`UncertaintyState._uncertainty_retains_volume`, `IntegrityStatus`) | `tests/contract/test_invariants.py::test_enum_rejects_unknown`, `::test_model_rejects_unknown_states` |
| Telemetry cannot reach planner, dispatcher or the write model (no automatic reservation release) | `.importlinter` contract `telemetry-cannot-command` | CI job `architecture` (`uv run lint-imports`); `tests/security/test_import_linter_bites.py` proves the engine reports `BROKEN` for injected violations |
| Facilities cannot own telemetry truth or the vehicle link | `.importlinter` contract `facilities-has-no-telemetry-truth` | CI job `architecture` |

**Gaps (not enforced anywhere):** device identity/attestation and per-observation signing
are not implemented — there is no ingest authentication, because there is no ingest
endpoint in Phase 1 (`POST` bodies are validated by Pydantic only). Plausibility limits
against the aircraft's last known state (max plausible velocity/jump) are not implemented.
Speed-of-light / replay-window checks against a trusted clock are not implemented. These
belong to the Phase 5 gateway and require an ADR before any live feed is connected.

---

## 2. Duplicate command or duplicate commit

**Threat.** A retried request (network flake, impatient operator, at-least-once queue)
creates two reservations, two launches, or a second commit of an already-committed volume.

**Design response.** Every state-changing call is keyed, and a duplicate is *rejected*,
not silently absorbed. The event stream is ordered so a replay cannot re-commit.

**Enforced by:**

| Control | Where | Proof it bites |
|---|---|---|
| `Idempotency-Key` header is mandatory on every `POST` in the contract | `contracts/openapi/v1.yaml` (25 operations) | `tests/contract/test_no_command_surface.py::test_idempotency_key_required_on_every_post` |
| Duplicate commit returns `409` with the conflict schema | `contracts/openapi/v1.yaml`, `packages/contracts/src/droneops_contracts/errors.py` | `tests/contract/test_no_command_surface.py::test_conflict_returns_409_on_duplicate_commit_posts`, `tests/contract/test_openapi_surface.py::test_conflict_schema_defines_409_body` |
| Repeating the same request is a no-op at the API boundary | `apps/api/src/droneops_api/main.py` | `tests/test_api_boundaries.py::test_simulated_order_is_idempotent` |
| A committed overlap on one volume is refused by the database, not by application arithmetic; a second tenant's duplicate key is not blocked by the first's | `packages/geom-core/src/droneops_geom_core/resources.py` (`lock_token` ordering), `tests/concurrency/test_postgres_reservation_invariants.py` | `::test_committed_overlap_on_one_volume_is_rejected_by_the_database`, `::test_duplicate_request_is_rejected_but_a_second_tenant_is_not` (run in CI job `migrations` against real PostGIS) |
| Immutable event ordering key + closed 23-event catalogue | `packages/contracts/src/droneops_contracts/events.py`, `contracts/events/` | `tests/contract/test_events.py::test_catalogue_has_23_events`, `::test_stream_ordering_key_is_present`, `::test_envelope_rejects_unknown_event_type` |
| Commitment token is a required contract field | `packages/contracts/src/droneops_contracts/reservation.py` | `tests/contract/test_schema_sync.py` |

**Gaps (not enforced anywhere):** server-side idempotency *storage* (a key cache with a TTL)
is not implemented — the contract mandates the header and the API echoes intent, but the
deduplicating store depends on the persistence layer still being built. Database `UNIQUE`
constraints over `packages/persistence/` migrations are **not yet committed**: there is no
`packages/persistence/alembic.ini`, so CI's `migrations` job runs its documented no-op
branch and `tests/concurrency` skips one test with "Alembic baseline is not committed yet".
Queue-level exactly-once semantics (Valkey/Celery redelivery fencing) are not implemented.

---

## 3. Tenant data leak

**Threat.** One operator's orders, corridors, facilities or telemetry become visible in
another operator's query, plan, log line or export.

**Design response.** Tenant is carried in every aggregate; resource keys are an allowlist;
the API cannot open a database connection, so there is no path from a request handler to an
arbitrary query.

**Enforced by:**

| Control | Where | Proof it bites |
|---|---|---|
| API cannot import the write model at all | `.importlinter` contract `api-has-no-command-path` (forbids `droneops_persistence`, `droneops_gateway`, `droneops_sim_runner`, the MAVLink adapter) | `tests/security/test_import_linter_bites.py::test_lint_imports_fails_on_synthetic_violation[api...]`; CI job `architecture` |
| Domain/contract layer has no ORM or framework, so no raw query can be smuggled into a domain object | `.importlinter` contract `domain-is-pure` | CI job `architecture` |
| Closed resource-key grammar: unknown kinds and malformed keys are rejected before they reach storage | `packages/geom-core/src/droneops_geom_core/resources.py` | `tests/contract/test_invariants.py::test_allowed_resource_keys_accept`, `::test_bad_resource_keys_reject` (`"unknown_kind:x"`, `"nope"`, `"airspace:y"`, `""`), `::test_reservation_kind_must_match_key` |
| Tenant isolation is exercised at the database, including the negative case | `tests/concurrency/test_postgres_reservation_invariants.py` | `::test_duplicate_request_is_rejected_but_a_second_tenant_is_not`, `::test_separate_volumes_never_collide` |
| Cross-tenant behaviour cannot be hidden by a fixture: an aircraft-state/volume test set is paired against tenant-scoped factories | `packages/testing-support/src/droneops_testing_support/factories.py` | `tests/contract/test_property.py`, `tests/contract/test_invariants.py` |

**Gaps (not enforced anywhere):** PostgreSQL **row-level security policies do not exist
yet** — `grep -rn "ROW LEVEL SECURITY\|current_setting" packages/persistence` returns
nothing, because migrations are not committed. There is no query allowlist checker. There
is no log scrubbing implementation or test: nothing redacts tenant identifiers from
structured logs, and `packages/observability/` has no scrubbing contract
(the only guard there is import-level: `observability-cannot-mutate-reservations`).
Capability scopes exist as an `apiKey` security scheme declaration in
`contracts/openapi/v1.yaml` but no authorisation logic enforcing per-tenant scopes.

---

## 4. A developer switches the simulator to live

**Threat.** Someone flips one configuration value — an endpoint URL, an env var, a
dependency — and the code under test starts talking to real airspace services or a real
aircraft.

**Design response.** Make the live path *absent from the import graph*, so there is no
switch to flip. The simulation kernel is forbidden from importing anything that can open a
socket, and the vehicle adapter is forbidden from importing anything that has opinions.

**Enforced by:**

| Control | Where | Proof it bites |
|---|---|---|
| `droneops_sim_core` and `droneops_sim_runner` may not import `sqlalchemy`, `asyncpg`, `httpx`, `requests`, `socket`, `uvicorn`, persistence, or any live adapter | `.importlinter` contract `sim-core-has-no-network-or-database` | CI job `architecture`; `tests/security/test_import_linter_bites.py` (`"Simulation kernel has no database"` reported `BROKEN` when `import droneops_persistence` is written into `packages/sim-core/src/droneops_sim_core/_importlinter_canary.py` and the build refuses it) |
| No live-endpoint switch exists to flip: `grep -rn "environ\|getenv\|DRONEOPS_" packages/sim-core apps/sim-runner` returns nothing | `packages/sim-core/`, `apps/sim-runner/` | verified 2026-10-01; means the Phase 0 "do not proceed if the boundary can be bypassed by an environment variable" bar is met by absence |
| The command module itself may not exist in the source tree; `scripts/check_boundaries.py` scans path components and route tokens | `scripts/check_boundaries.py` (`FORBIDDEN_PATH_TOKENS`, `FORBIDDEN_ROUTE_TOKENS`) | `tests/security/test_command_path_detectors.py::test_command_module_in_the_source_tree_is_detected`, `::test_command_route_is_detected_on_both_sides`, `::test_legitimate_simulation_routes_are_not_flagged` |
| Deleting or renaming one contract line in `.importlinter` is caught, so the boundary cannot be quietly weakened in review | `scripts/check_boundaries.py` (declared-contract check) | `tests/security/test_command_path_detectors.py::test_deleting_one_contract_line_is_caught`, `::test_renaming_a_contract_is_caught`, `::test_boundary_contracts_are_declared_in_the_real_repo` |
| The gateway is a placeholder with no command directory, and the MAVLink adapter cannot reach any DroneOps domain package | `apps/gateway/`, `adapters/vehicle-mavlink/`, `.importlinter` contract `mavlink-adapter-is-dependency-free` | CI job `architecture` |
| A command route in the OpenAPI breaks the build | `scripts/openapi_gate.py` | CI job `canary-forbidden-import`, step "a command route must fail the OpenAPI gate" (injects `/v1/missions/{id}/launch`, asserts `openapi_gate.py` exits non-zero) |

**Gaps (not enforced anywhere):** signed deployment manifests do not exist. Separate IAM
roles per environment do not exist (no cloud account is provisioned). Nothing prevents a
developer from writing a *new* script outside the import graph that calls MAVLink from the
command line — that is a human-process control (CODEOWNERS on `/adapters/`, plus the
Phase 5 ADR gate), not a build control.

---

## 5. Stale route or restriction data

**Threat.** Planning proceeds on a NOTAM, airspace boundary or terrain model whose source
has expired, silently degrading a safety decision into a guess.

**Design response.** Every external input carries an expiry; an expired input fails closed
(no launch) rather than being used with a warning. Inputs are frozen into a hashed bundle
so the plan under review is the plan that ran.

**Enforced by:**

| Control | Where | Proof it bites |
|---|---|---|
| `Freshness` is a closed enum (`fresh`/`stale`/…) and `Provenance.is_expired(now)` is part of the contract model | `packages/contracts/src/droneops_contracts/provenance.py` | `tests/contract/test_invariants.py::test_enum_rejects_unknown`, `tests/contract/test_events.py` |
| Expired restriction data refuses to run the scenario | `apps/sim-runner/src/droneops_sim_runner/bundle.py` | `tests/unit/test_scenario_bundle_manifest.py::test_expired_restriction_data_fails_closed` |
| A bundle that claims real authority is refused outright | `bundle.py` | `::test_a_bundle_claiming_authority_is_refused` |
| Editing any frozen input file after recording is refused; the manifest declares every file | `apps/sim-runner/.../bundle.py`, `hashing.py` | `::test_editing_a_bundle_file_is_refused`, `::test_manifest_declares_every_file`, `::test_bundle_manifest_hash_is_stable` |
| Replay must be byte-identical to the committed golden, so drift in source data is caught by diff rather than by inspection | `simulation/golden/chandigarh-basic/` | CI job `simulation` (`droneops-sim replay`), `::test_golden_reference_matches_the_current_bundle`, `::test_same_bundle_twice_produces_the_same_events` |
| Deterministic time base: no wall-clock drift can make stale data look fresh | `packages/geom-core/src/droneops_geom_core/timebase.py` | `tests/unit/test_deterministic_time_and_release_hash.py::test_frozen_clock_never_moves_and_demands_a_timezone`, `::test_time_is_integer_microseconds` |
| Configuration release is content-addressed, so a plan pins the exact data version it used | `scripts/config_release.py` | `::test_config_release_digest_is_canonical_and_content_sensitive`, `::test_config_release_digest_is_stable_across_calls`; `tests/contract/test_invariants.py::test_config_release_sha_required_pattern` |

**Gaps (not enforced anywhere):** there is no live source reconciliation job (nothing
compares two independent airspace feeds), no freshness UI in `apps/console/` that refuses
to render a stale plan, and no scheduled re-import with an expiry alarm. In Phase 1 all
restriction data is synthetic, so the only honest control is the fail-closed expiry test.

---

## 6. Supply-chain dependency compromise

**Threat.** A malicious or compromised version of a dependency — or a build action — enters
the runtime that plans reservations.

**Design response.** Pin everything, install only from locks, publish an SBOM, refuse
unknown licences, and audit the pins for known CVEs.

**Enforced by:**

| Control | Where | Proof it bites |
|---|---|---|
| Python lockfile is committed and installs are frozen; its digest is emitted per run | `uv.lock` (tracked: `git ls-files uv.lock` → `uv.lock`), CI job `workspace` (`uv sync --frozen --all-packages`, `sha256sum uv.lock`) | a drifted lock fails CI with `--frozen` |
| Node install is `npm ci` from a lockfile only | `apps/console/package-lock.json`, CI jobs `workspace` and `ui` | `npm ci` fails on any lock/manifest mismatch |
| Licence allowlist is MIT/BSD/Apache-2.0/ISC with individually reviewed exceptions | `scripts/check_licences.py`, `configs/licensing/licence-allowlist.txt`, `configs/licensing/licence-exceptions.txt` | CI job `security`; local hook `licences` in `.pre-commit-config.yaml` |
| Known-vulnerability scan is strict | CI job `security` (`uv run pip-audit --progress-spinner off --strict`), `make audit` | fails the job on any advisory |
| SBOM is produced and uploaded every build | CI job `images` (`uv export --frozen` → `cyclonedx-py`, artifact `sbom`), `make sbom` | artifact present per run |
| CI actions and hook revisions are pinned to explicit tags | `.github/workflows/ci.yml` (`actions/checkout@v5`, `astral-sh/setup-uv@v6` with `version: "0.11.2"`, `actions/setup-node@v5`, `actions/upload-artifact@v4`, `gitleaks/gitleaks-action@v2`), `.pre-commit-config.yaml` (pinned `rev:` for all five repos, with a "never `--autoupdate`" note) | review-level, enforced by CODEOWNERS on `/.github/` and `/.pre-commit-config.yaml` |
| Images build non-root or the build fails | `apps/api/Dockerfile`, `apps/console/Dockerfile`, CI job `images` ("images run as non-root") | explicit `exit 1` in that step |
| A dependency owner is named | `.github/CODEOWNERS` (`/configs/ @krisgarg25`, `/.github/ @krisgarg25`) | review requirement |

**Gap found and fixed while writing this model:** `.pre-commit-config.yaml` pinned
`gitleaks/gitleaks` at `rev: v8.24.5`, **which does not exist upstream** (`git ls-remote
--tags` lists only v8.24.0–v8.24.3). The whole pre-commit configuration failed to
initialize, meaning the local secret-scan hook had never actually run. Corrected to
`rev: v8.24.3`; `pre-commit run gitleaks --all-files` now reports
`Detect hardcoded secrets...Passed`. This is exactly the failure mode this threat
describes — a pin that cannot be verified silently stops protecting.

**Gaps (not enforced anywhere):** no `dependabot`/Renovate configuration exists. No
provenance attestations or signed container images (cosign/sigstore) exist — images are
built and tagged, never signed. No reproducibility check that a rebuilt image matches the
published digest. `pip-audit` catches known CVEs only, not a novel malicious release.

---

## 7. Operational denial through queue flooding

**Threat.** A client (or a compromised client) floods the request path or the job queues so
safety-relevant work — conflict checks, reservation commits, telemetry expiry — is starved,
turning a denial of service into an unsafe operating state.

**Design response (intended).** Bounded request size, per-tenant rate budgets, a priority
lane for safety events, and a circuit breaker that refuses new work rather than queueing it.

**What is actually enforced today:**

| Control | Where | Proof it bites |
|---|---|---|
| Every request body is a strict, closed Pydantic model; unknown fields are rejected, so a flood cannot smuggle unbounded structures into the domain layer | `packages/contracts/src/droneops_contracts/*` (`ContractModel`), `contracts/json-schema/` | `tests/contract/test_invariants.py::test_model_rejects_unknown_states`, `tests/contract/test_schema_sync.py::test_schema_matches_model` |
| The API has no path to the write model or the vehicle link, so flooding `/v1/**` cannot amplify into a commit storm beyond simulation state | `.importlinter` contract `api-has-no-command-path` | CI job `architecture` |
| The worker is a separate process and cannot be reached by calling the API (no in-process queue that a request flood can fill) | `.importlinter` contract `worker-has-no-api-import` | CI job `architecture` |
| Deterministic, integer-ordered event execution: a flooded scenario cannot reorder safety events by clock noise | `apps/sim-runner/`, `packages/geom-core/.../timebase.py` | `tests/test_safety_boundaries.py::test_simulation_events_execute_in_integer_time_order` |
| Test-suite backpressure exists at the tooling level only: per-test timeout 120 s, strict markers/warnings as errors | `pyproject.toml` (`timeout = 120`, `filterwarnings = ["error::..."]`) | every `pytest` run |

**Gaps — this threat is the least covered of the seven, and the gap is real:**

- **No rate limiting exists.** `grep -rn "limit\|Rate\|middleware" apps/api/src` returns
  nothing; there is no ASGI middleware, no per-token bucket, no 429 response in
  `contracts/openapi/v1.yaml`.
- **No request-size cap** is configured in `apps/api/src/droneops_api/main.py` or in the
  Dockerfiles/Compose.
- **No tenant budgets** and **no safety-event priority lanes** exist; `apps/worker/` is a
  stub package (`apps/worker/src/droneops_worker/__init__.py` is the only module) with no
  queue, so there is nothing to prioritise.
- **No circuit breaker**, no queue-depth metric, no alert on backlog depth;
  `packages/observability/` exposes structured logging only.
- Valkey is defined in `infra/compose/docker-compose.yml` but is not used by any code path,
  so queue behaviour is currently untested.

Required before Phase 2 merges any queue work: a rate-limit + request-size test in
`tests/contract/`, a queue-depth alert as specified in
`technical-build/07-infrastructure-ci-security.md` section 10, and an ADR for the priority
lane. Until then this model records the threat as **unmitigated**, not mitigated.

---

## What this model does not cover

State this plainly to anyone reading the repository:

1. **A read-only technical boundary is not an operating approval.** Everything above proves
   that *this build cannot send a command*, and that certain unsafe changes make the build
   red. It proves nothing about whether any flight would be safe, legal or sane. No
   authority has been consulted, no certification has been sought, no airspace user
   agreement exists, and no real vehicle, crew, passenger or cargo is represented by any
   data in this repository. "All gates green" is a statement about code hygiene.
2. **The aircraft's onboard geofence and failsafes remain the primary safety layer.** If a
   live link is ever added, this software is an optimisation and an audit trail sitting
   *behind* the vehicle's own altitude/area fences, return-to-home, link-loss and
   battery failsafes — which must be able to act without, and against, anything proposed
   here. Onboard systems are not verified by this repository and this document must never
   be cited as evidence that they are.
3. **Physical, human and organisational threats are out of scope:** insider misuse by an
   authenticated operator, social engineering, facility physical security, weather hazards,
   GNSS jamming/spoofing at the radio layer, crew fatigue, maintenance error, and the
   correctness of the geometry/routing kernels as *physics* (a passing test suite does not
   mean the model matches the world).
4. **Runtime availability is out of scope.** The queue-flooding mitigation above is not
   implemented, so this model does not claim any denial-of-service resistance for a running
   deployment.
5. **Data authenticity is out of scope.** Synthetic fixtures are frozen and hashed; that is
   integrity of *our* copy, not verification of an authority's NOTAM.
6. **This model is not a penetration test.** No adversarial exercise has been performed
   against a deployed system, because none is deployed.

Live aircraft integration, when it happens, is governed by
`technical-build/10-aircraft-integration-boundaries.md` and requires its own ADR; nothing
in this file pre-approves it.
