# Phase 0 exit-gate report

Run date: 2026-10-01. Repository: `droneops` at `HEAD = e475c22`, branch `main`,
**no git remote configured** (`git remote -v` is empty). Host: Windows 11, Git Bash,
GNU make available only as `/c/MinGW/bin/mingw32-make` (bare `make` is not on `PATH`).
Commands were run with `uv run --no-sync` against the existing environment; no dependency
was added or re-locked.

> **Caveat that applies to every number below.** Other agents were editing this working tree
> while this report was produced, and the suite result changed three times in about twenty
> minutes without any change from me:
>
> | observed | `pytest -q --no-cov` | `lint-imports` |
> |---|---|---|
> | early | `1 failed, 736 passed, 8 skipped` — `FAILED packages/geom-core/tests/test_properties.py::test_safe_intervals_equal_horizon_minus_blocked` | aborted: `Syntax error in packages/persistence/src/droneops_persistence/types.py, line 204: ]` (half-saved file) |
> | middle | `741 passed, 4 skipped` | `Analyzed 93 files, 294 dependencies. Contracts: 18 kept, 0 broken.` |
> | final | `2 failed, 739 passed, 4 skipped` — including `FAILED tests/unit/test_scenario_bundle_manifest.py::test_golden_reference_matches_the_current_bundle` | `Analyzed 102 files, 337 dependencies. Contracts: 18 kept, 0 broken.` |
>
> The failures are in files owned by other agents (`packages/geom-core`,
> `packages/sim-core`/`apps/sim-runner`, `packages/persistence`, `simulation/golden`) and are
> the expected noise of concurrent work, but they mean **the "737 passing tests" figure in
> the Phase 0 brief is not reproducible at any single instant right now**, and no gate number
> in this report should be treated as a baseline. `lint-imports` reported `18 kept, 0 broken`
> on every invocation that could parse the tree. Re-run everything against a clean, committed
> tree before anyone signs the gate.

Sources of the criteria: `technical-build/05-implementation-phases.md` ("Phase 0 … Exit
criteria") and `technical-build/08-repo-bootstrap-checklist.md` section 17.

---

## Part A — the five Phase 0 exit criteria

### A1. "A new developer can run `make bootstrap`, `make test` and `make dev` on Windows, macOS and Linux"

**NOT MET.** Two distinct failures.

`make dev` does not exist. `Makefile` has no `dev` rule (`grep -n '^dev' Makefile` → no
match; `.PHONY` even names a `dev-env` target that also has no rule):

```text
$ mingw32-make dev
mingw32-make: *** No rule to make target 'dev'.  Stop.
exit=2
```

Full target list from `mingw32-make help` contains: `help bootstrap db-up db-down db-status
db-clean migrate api worker test unit imports lint format typecheck sim replay sim-freeze
console-install console openapi-diff licence audit sbom sec check e2e-install clean-hooks`.
There is no `dev`, and no aggregate "run everything locally" target. `make api`, `make
worker`, `make console` and `make db-up` exist individually and are the intended parts.
**The Makefile is owned by another agent, so no target was invented here.** Required fix
(one rule, in their scope): `dev: db-up` style composition of `api` + `worker` + `console`
plus documentation of which shell provides `make` on each OS.

Second failure: on this Windows host `make` itself is absent from `PATH`
(`command -v make` → nothing; only `mingw32-make` resolves), so the literal command in the
exit criterion cannot be run as written without a `make` shim.

`make bootstrap` and `make test` were **not executed end-to-end** and are therefore not
claimed as met: `bootstrap` installs hooks into the shared `.git` directory and runs
`npm ci` while other agents are committing, which would have disturbed their work. Their
constituent commands were all run successfully and independently:

| Part of `bootstrap` | Command actually run | Result |
|---|---|---|
| `uv sync --frozen --all-packages` | not re-run (environment already synced from the committed `uv.lock`) | CI job `workspace` runs it; lock verified in A4 |
| `pre-commit install --install-hooks` | see Part C (secret scan) | hook environment install now works; it did **not** work before the `gitleaks` rev fix |
| `python scripts/check_boundaries.py` | `uv run --no-sync python scripts/check_boundaries.py` | `no command artifacts, no command routes, all boundary contracts declared` |
| `make console-install` | not run (npm ci) | — |

`make test` is `uv run pytest`; that exact command was run and is reported in A2/E1.

macOS and Linux behaviour was **not** tested: only Windows was available.

### A2. "CI fails if the API imports `gateway` or the sim runner imports persistence secrets"

**MET** (locally verified; the GitHub Actions run itself was not observed — no remote).

Three independent mechanisms, all executed:

1. Import contracts, clean tree:

   ```text
   $ uv run --no-sync lint-imports
   Analyzed 93 files, 294 dependencies.
   Contracts: 18 kept, 0 broken.
   ```

   Relevant contracts in `.importlinter`: `api-has-no-command-path` (forbids
   `droneops_gateway`, `droneops_persistence`, `droneops_sim_runner`,
   `droneops_adapters_vehicle_mavlink`) and `sim-core-has-no-network-or-database`
   (forbids `droneops_persistence`, `sqlalchemy`, `asyncpg`, `httpx`, `requests`, `socket`,
   `uvicorn` for `droneops_sim_core` **and** `droneops_sim_runner`).

2. The build is asserted to *fail* on a deliberate violation — a real non-zero exit
   assertion, not a lint report:

   ```text
   $ uv run --no-sync pytest tests/security -m security --no-cov -q -o addopts=""
   12 passed in 1.65s
   ```

   `tests/security/test_import_linter_bites.py` writes
   `from droneops_gateway import nothing` into `apps/api/src/droneops_api/` and
   `import droneops_persistence` into `packages/sim-core/src/droneops_sim_core/`, runs
   `lint-imports --no-cache`, and asserts `returncode != 0`, `"BROKEN"` in output and the
   named contract in output — then asserts a clean tree returns 0. Reproduced manually:

   ```text
   $ echo "from droneops_gateway import telemetry_decoder" > apps/api/src/droneops_api/_canary_forbidden_import.py
   $ uv run --no-sync lint-imports --no-cache ; echo exit=$?
   exit=1            # and, before the file was written, exit=0
   ```

3. CI job `canary-forbidden-import` in `.github/workflows/ci.yml` performs the same
   injection at pipeline level and `exit 1`s the job if `lint-imports` ever allows it.

### A3. "The OpenAPI has no vehicle write command"

**MET.** The complete write surface of `contracts/openapi/v1.yaml` (25 operations, listed by
`uv run --no-sync python -c …`):

```text
POST   /v1/orders                                        createOrder
POST   /v1/orders/{id}/cancel                            cancelOrder
POST   /v1/missions/{id}/plan                            requestMissionPlan
POST   /v1/missions/{id}/reserve                         reserveMissionResources
POST   /v1/missions/{id}/cancel                          cancelMission
POST   /v1/missions/{id}/proposals/{proposal_id}/preview previewMissionProposal
POST   /v1/missions/{id}/proposals/{proposal_id}/accept  acceptMissionProposal
POST   /v1/missions/{id}/proposals/{proposal_id}/decline declineMissionProposal
POST   /v1/facilities/{id}/outage                        reportFacilityOutage
GET    … (16 read operations: orders, missions, aircraft, facilities, reservations,
        audit-events, simulations, replay-diff, scenario compare, decision trace,
        uncertainty, observations, plan-versions)
```

No `PUT`/`PATCH`/`DELETE` exists at all, and every write is control-plane lifecycle
(cancel, plan, reserve, accept/decline a proposal, report an outage). Nothing launch-,
takeoff-, land-, return-, goto-, position-upload- or command-shaped is present. Enforced by
`scripts/openapi_gate.py` (`FORBIDDEN_ROUTE_TOKENS` in `scripts/check_boundaries.py`) and by
`tests/contract/test_no_command_surface.py`:

```text
$ uv run --no-sync python scripts/openapi_gate.py --report data/reports/openapi-diff.json
contract routes: 25, implemented: 0, planned: 25, undeclared: 0
no command route, no undeclared route
```

Note `implemented: 0`: the contract surface is fully declared but **no route is served by
`apps/api` yet** — that is Phase 4 work, not a Phase 0 violation, but it means the API-side
assertion currently runs against an empty implemented set.

### A4. "Repository is private, backup policy is documented and no live credentials exist"

**PARTLY UNMET — two of three cannot be evidenced from this machine.**

- *Private*: **unverifiable / false as a claim.** `git remote -v` prints nothing; the
  repository exists only on this disk. "Private" and "branch-protected" are properties of a
  hosting account that has not been created. `.github/CODEOWNERS` exists and names
  `@krisgarg25`, and its own header states that the required `required_approving_review_count: 2`
  "must be configured in repository settings … and must be verified with a test PR" — no PR
  has been made because there is no remote.
- *Backup policy documented*: **not met in-repo.** `grep -rli backup --include=*.md .`
  returns nothing. The policy exists only as design text in
  `technical-build/07-infrastructure-ci-security.md` section 13, which lives outside this
  repository and is not an operational backup procedure. A `docs/backup-policy.md` (or a
  section in `docs/conventions.md`) is still owed.
- *No live credentials*: **met.** `.env` exists on disk and is **not** tracked
  (`git ls-files .env` → empty; `git ls-files | grep ^.env` → `.env.example` only). Secret
  scan is clean — see Part C. The only literal values matching credential patterns are
  documented placeholders: `.env.example:POSTGRES_PASSWORD=local-development-only`,
  `ci.yml: POSTGRES_PASSWORD: ci-only-not-a-secret`, and
  `docker-compose.yml: ${POSTGRES_PASSWORD:-local-development-only}`.

### A5. "Configuration-release manifest can be hashed"

**MET.**

```text
$ uv run --no-sync python scripts/config_release.py
b3b1d46bff4f2531aa1d651c6dffea0b57ce2f243e46333be6bc4f39e40dcad8 (39 files)
$ uv run --no-sync python scripts/config_release.py        # second invocation
b3b1d46bff4f2531aa1d651c6dffea0b57ce2f243e46333be6bc4f39e40dcad8 (39 files)
```

Digest is stable across runs and covers `configs/` + `contracts/` canonically
(`DEFAULT_INPUTS`), with per-file entries and a `--json --out` form. `make sim-freeze`
records the bundle digests into the scenario manifest. Asserted by
`tests/unit/test_deterministic_time_and_release_hash.py::test_config_release_digest_is_canonical_and_content_sensitive`
and `::test_config_release_digest_is_stable_across_calls`, and by
`tests/contract/test_invariants.py::test_config_release_sha_required_pattern` and
`::test_run_identity_config_release_pattern` (every run/plan/decision must carry
`config_release_sha256`). Scenario-manifest hashing itself is covered by
`tests/unit/test_scenario_bundle_manifest.py::test_bundle_manifest_hash_is_stable`.

### A6. "Do not proceed if the build boundary can be bypassed by changing an environment variable"

**MET by absence — checked specifically.**

```text
$ grep -rn "environ\|getenv\|DRONEOPS_" --include=*.py packages/sim-core apps/sim-runner
(no output)
```

The simulation kernel and runner read no environment at all, and `.importlinter` forbids
them from importing `socket`, `httpx`, `requests`, `uvicorn`, `sqlalchemy`, `asyncpg` or any
live adapter, so there is no variable that converts them to a live client. The remaining
env-var surface (`POSTGRES_*`, `DATABASE_URL`) selects a *database*, not a vehicle link, and
`droneops_api` is contractually barred from `droneops_persistence`.

---

## Part B — `08-repo-bootstrap-checklist.md` section 17 acceptance checklist

| # | Item | Verdict | Evidence actually executed / filed |
|---|---|---|---|
| 1 | private repository with defined owners | **unmet (owner yes, privacy no)** | `.github/CODEOWNERS` present, `* @krisgarg25` plus per-path safety ownership; but `git remote -v` empty → not hosted, not private, not branch-protected |
| 2 | reproducible lockfiles | **met** | `git ls-files uv.lock` → `uv.lock`; CI installs with `uv sync --frozen --all-packages`; `apps/console/package-lock.json` + `npm ci` in jobs `workspace`/`ui` |
| 3 | clean Docker and Python environment | **partly met** | `apps/api/Dockerfile`, `apps/console/Dockerfile` exist and CI job `images` asserts non-root user; `.dockerignore` present. `docker build` was **not** run here (no daemon verification), and `infra/compose/docker-compose.yml` `up` was not exercised by this agent |
| 4 | health API and local Postgres | **met (API), met-by-config (Postgres)** | `tests/test_api_boundaries.py::test_health_declares_simulation_only` passes in the suite; `make db-up`/`db-status` target `postgres` + `valkey`; concurrency tests ran against real Postgres during the final suite run (only 1 concurrency test now skips, for the missing Alembic baseline) |
| 5 | contract package exists | **met** | `packages/contracts/src/droneops_contracts/` + `contracts/openapi/v1.yaml` + `contracts/json-schema/` (synced by `tests/contract/test_schema_sync.py`) |
| 6 | no live aircraft code | **met** | `uv run --no-sync python scripts/check_boundaries.py` → `no command artifacts, no command routes, all boundary contracts declared`; `adapters/vehicle-mavlink/` is dependency-free per contract `mavlink-adapter-is-dependency-free` |
| 7 | no command route | **met** | see A3, `openapi_gate.py` output |
| 8 | import boundary tests pass | **met** | `lint-imports`: `18 kept, 0 broken`; `pytest tests/security -m security` → `12 passed` |
| 9 | CI canary proves a forbidden import fails | **met in content, was broken in sequence, now fixed** | see Part C — a latent ordering bug made the canary job red on every run; the cleanup line was added and the sequence re-verified |
| 10 | scenario manifest hash is stable | **met** | `scripts/config_release.py` digest identical across two runs; `test_bundle_manifest_hash_is_stable`, `test_golden_reference_matches_the_current_bundle` pass |
| 11 | README explains Phase 1 is simulation | **met, with one misstatement corrected** | see Part D |
| 12 | legal and privacy owner named | **partly met** | only one owner (`@krisgarg25`) is named anywhere in CODEOWNERS; there is no distinct legal/privacy owner and no named role in `docs/`. `configs/licensing/licence-exceptions.txt` requires human review per entry but does not say by whom |
| 13 | repository backed up or branch-protected | **unmet** | no remote → neither. This is the same root cause as item 1 |
| 14 | no secrets or customer data in examples | **met** | gitleaks clean (Part C); `.env.example` values are placeholders and commented as such; fixtures use opaque ids (`packages/testing-support/.../factories.py`), and `security/phase-1-boundaries.md` bans real customer names |
| 15 | ADRs explain the major trade-offs | **met** | `adr/0001-modular-monolith.md` … `adr/0008-vehicle-link-isolation.md` (8 ADRs) covering the monolith, simulation-only Phase 1, the Postgres transactional boundary, no-command-path, scenario bundles, the independent checker, synthetic data and vehicle-link isolation |

---

## Part C — CI canary: is it a build failure or just a lint report?

**It is a genuine build-failure assertion, in two places.**

1. **CI job** `canary-forbidden-import` (`.github/workflows/ci.yml`, comment "11 canary:
   prove a bite"). Each step injects a violation and asserts the *command exits non-zero*,
   `exit 1`-ing the job if the tool let it through:

   ```yaml
   - name: canary - API importing the vehicle gateway must fail the build
     run: |
       echo "from droneops_gateway import telemetry_decoder  # deliberate canary" \
         > apps/api/src/droneops_api/_canary_forbidden_import.py
       if uv run lint-imports --no-cache; then
         echo "::error::import-linter allowed the API to reach the vehicle gateway"
         exit 1
       fi
   ```

   plus a second step that injects `/v1/missions/{id}/launch` into
   `contracts/openapi/v1.yaml` and requires `scripts/openapi_gate.py` to fail, and a third
   that requires `droneops_sim_core` → `droneops_persistence` to fail. The job is a
   dependency of the aggregate `required` gate, so it blocks merge. **Not lint-only.**

2. **Test-level canary** `tests/security/test_import_linter_bites.py` asserts
   `result.returncode != 0` and `"BROKEN" in output` and the specific contract name, for four
   violations (gateway-in-API, sqlalchemy-in-energy, persistence-in-sim-core,
   planner-in-conflict-checker), and asserts the clean tree returns 0. It is wired into CI as
   the `unit` job step "import-linter bites on a synthetic violation"
   (`uv run pytest tests/security -m security`).

**Bug found and fixed.** In the original job, step 1 wrote
`apps/api/src/droneops_api/_canary_forbidden_import.py` and **never removed it**. Step 3
ended with `rm -f <sim-core canary>` followed by an unconditional `uv run lint-imports
--no-cache` intended to prove the tree is clean again — but the API canary from step 1 was
still in the tree, so that assertion would fail and `canary-forbidden-import` (and therefore
the `required` gate) would be **red on every CI run**, including runs with no violation at
all. Confirmed empirically: with only the API canary present, `lint-imports --no-cache`
exits `1`. Fix applied to `.github/workflows/ci.yml`: step 1 now removes its canary file
(before both the failure branch and the success branch) and re-asserts a clean tree itself.
Replaying the corrected sequence locally:

```text
STEP1 OK: build correctly failed   (lint-imports exit=1 with the injected import)
STEP1 cleanup OK: tree clean again (lint-imports exit=0 after rm)
```

The `if`-guard in step 1's error branch also now cleans up, so a future regression cannot
leave a poisoned tree for the next step. No other job was reordered.

---

## Part D — README accuracy

`README.md` said under "What is intentionally absent": `database`. That is now false —
`packages/persistence/`, `infra/compose/docker-compose.yml` with PostGIS, and `make db-up` /
`make migrate` all exist. Corrected to name the actual state (a local PostGIS for
simulation data, still no production/cloud database). Its safety lines were left intact and
verified: "Phase 1 is simulation and control-plane support only", "cannot launch, land,
return, upload or command aircraft", "Onboard aircraft fences and failsafes remain the
primary safety layer". No claim of approval, aircraft integration or live data was added or
left anywhere; the README gained only pointers to `security/threat-model.md` and this report.
The README does not advertise `make dev`, so the missing target is not a documentation lie —
it is a genuine Makefile gap (A1).

---

## Part E — raw verification block

```text
$ uv run --no-sync pytest -q --no-cov
.........................                                                [100%]
============================== warnings summary ===============================
.venv\Lib\site-packages\fastapi\testclient.py:1:
  StarletteDeprecationWarning: Using `httpx` with `starlette.testclient` is deprecated;
  install `httpx2` instead.
-- Docs: https://docs.pytest.org/stable/how-to/capture-warnings.html

# the default `addopts` includes `--cov=.` which suppresses the terminal summary line at
# this verbosity, so counts were taken with addopts cleared (same tests, no coverage):
$ uv run --no-sync pytest -q --no-cov -o addopts="" --tb=line -p no:cacheprovider
741 passed, 4 skipped, 1 warning in 14.62s

# earlier in this session, against a mid-edit tree:
#   1 failed, 736 passed, 8 skipped, 1 warning in 14.14s
#   FAILED packages/geom-core/tests/test_properties.py::test_safe_intervals_equal_horizon_minus_blocked
# and on the last invocation of this report:
#   2 failed, 739 passed, 4 skipped, 1 warning in 14.61s
#   FAILED tests/unit/test_scenario_bundle_manifest.py::test_golden_reference_matches_the_current_bundle
# (both failure sets are in other agents' in-flight files; see the caveat at the top)

$ uv run --no-sync lint-imports
Analyzed 93 files, 294 dependencies.
Contracts: 18 kept, 0 broken.          # final invocation: Analyzed 102 files, 337 dependencies

$ uv run --no-sync pytest tests/security -m security --no-cov -q -o addopts=""
12 passed in 1.65s

# ---- secret scan ----
$ command -v gitleaks
(none — no gitleaks binary on this host)

$ uv run --no-sync pre-commit run gitleaks --all-files      # BEFORE fix
An unexpected error has occurred: CalledProcessError: ('git', 'checkout', 'v8.24.5')
    error: pathspec 'v8.24.5' did not match any file(s) known to git

$ git ls-remote --tags https://github.com/gitleaks/gitleaks | grep -o 'v8\.24\.[0-9]*' | sort -u
v8.24.0 v8.24.1 v8.24.2 v8.24.3          # v8.24.5 does not exist upstream

# fix applied to .pre-commit-config.yaml: rev: v8.24.5 -> v8.24.3
$ uv run --no-sync pre-commit run gitleaks --all-files      # AFTER fix
Installing environment for https://github.com/gitleaks/gitleaks...
Detect hardcoded secrets.................................................Passed

# independent pattern sweep over tracked files (232 files, 230 in scope), looking for
# private-key blocks, AWS AKIA-, GitHub ghp_/gho_, Slack xox-, Anthropic sk-ant-, Google
# AIza-, JWTs, and high-entropy literal assignments:
  hard credential signatures: 0
  high-entropy literal assignments: 0
  keyword-only matches (203) were all identifiers/comments: commitment_token, lock_token,
  geohash3d token, apiKey security-scheme name, POSTGRES_PASSWORD placeholders

# ---- hygiene ----
$ for p in .env .coverage coverage.xml .import_linter_cache/foo apps/console/node_modules/x \
           apps/console/dist/x simulation/reports/latest/a.json data/raw/x; do \
      git check-ignore -v "$p"; done
.env                                    .gitignore:1:.env
.coverage                               .gitignore:23:.coverage
coverage.xml                            .gitignore:24:coverage.xml
.import_linter_cache/foo                .gitignore:25:.import_linter_cache/
apps/console/node_modules/x             .gitignore:8:node_modules/
apps/console/dist/x                     .gitignore:9:dist/
simulation/reports/latest/a.json        .gitignore:26:simulation/reports/
data/raw/x                              .gitignore:13:data/raw/*

$ git ls-files uv.lock
uv.lock

$ git ls-files .env
(empty — only .env.example is tracked; a real .env exists on disk and is untracked)

$ git add --dry-run . | grep -i "CRLF\|warning"
(no output — line-ending conversion warnings are silent since .gitattributes)

# ---- gates ----
$ uv run --no-sync python scripts/openapi_gate.py --report data/reports/openapi-diff.json
contract routes: 25, implemented: 0, planned: 25, undeclared: 0
no command route, no undeclared route

$ uv run --no-sync python scripts/check_boundaries.py
no command artifacts, no command routes, all boundary contracts declared

$ uv run --no-sync python scripts/config_release.py   # twice
b3b1d46bff4f2531aa1d651c6dffea0b57ce2f243e46333be6bc4f39e40dcad8 (39 files)

# ---- make ----
$ command -v make
(none on PATH; only /c/MinGW/bin/mingw32-make)
$ mingw32-make dev
mingw32-make: *** No rule to make target 'dev'.  Stop.    (exit=2)
```

**Hygiene items reviewed but not changed:**

- `.gitattributes` exists with `* text=auto eol=lf` plus binary overrides, and `git add
  --dry-run` produces no CRLF warnings. **However it is still untracked**
  (`git status --porcelain` → `?? .gitattributes`). An untracked `.gitattributes` protects
  only this working copy; the moment the repo is cloned elsewhere, line-ending conversion
  resumes. It must be committed. It was not staged here because other agents are committing
  concurrently and staging files into a shared index would have swept their half-finished
  work in.
- **Docker volumes**: no `.gitignore` entry is needed and none was invented. Both
  `postgres` and `valkey` in `infra/compose/docker-compose.yml` use **named** volumes
  (`volumes:` at the document root), which live in Docker's own storage, not in the repo
  tree; there are no bind mounts of host paths into containers. The only path the compose
  file reads from disk is `../../.env`, already ignored.
- Stray file at the repository root: `cd` (0 bytes, untracked, created 2026-10-01 01:11 by
  an accidental redirection in someone's command). Harmless but it would be committed by
  `git add .`; delete it before the next commit. Not removed here because the tree is shared.

---

## Bottom line

**Genuinely met:** the boundary criterion (A2), the no-command-route criterion (A3), the
hashable configuration-release manifest (A5), the env-var-bypass bar (A6), and checklist
items 2, 5, 6, 7, 8, 10, 14, 15.

**Met only in content, not in execution:** every CI-side claim. The jobs and canaries exist
in `.github/workflows/ci.yml` and their underlying commands all pass locally, but nothing
has ever run on GitHub Actions: there is no remote, so "CI fails if…" is demonstrated by
reproducing the commands, not by observing a red build. Checklist items 1 and 13 fail for
the same reason.

**Unmet:**

1. **`make dev` does not exist** (A1) — the Phase 0 exit criterion is not satisfied as
   written, and `make` is not on `PATH` on the Windows host either. Ownership is with another
   agent; nothing was invented here.
2. **Bootstrap and backup claims** (A4, checklist 1/12/13) — no remote, no branch
   protection, no in-repo backup policy, no distinct legal/privacy owner.
3. **Queue-flooding threat is unmitigated** — no rate limiting, no request-size cap, no
   tenant budgets, no priority lanes, no circuit breaker. Recorded as a gap in
   `security/threat-model.md` section 7 rather than papered over.
4. **Persistence-layer controls pending** — no committed Alembic baseline
   (`packages/persistence/alembic.ini` absent, so CI's `migrations` job takes its documented
   no-op branch), no row-level security, no log scrubbing.
5. **`.gitattributes` untracked** — hygiene fix is on disk but not yet in history.
6. **Formatting gate is red against the working tree as it stands.**
   `uv run --no-sync ruff format --check .` reports
   `57 files would be reformatted, 93 files already formatted` — the unformatted files are
   the modules other agents are writing right now (`packages/sim-core`,
   `packages/persistence`, `adapters/gis-etl`, `apps/sim-runner`). CI job `lint` runs exactly
   that command, so the pipeline would fail today on formatting alone. Nothing I changed is
   involved: `pre-commit run --files` over the six files this task touched reports
   `detect private key...Passed`, `fix end of files...Passed`,
   `trim trailing whitespace...Passed`, `check yaml...Passed`,
   `Detect hardcoded secrets...Passed`, `import-linter architecture contracts...Passed`,
   `no command artifact and boundary contracts declared...Passed`.

**Phase 0 cannot be signed as exited.** The safety boundaries themselves are real,
executed and provably load-bearing; what is missing is the developer-entry-point target and
everything that requires a hosted repository.
