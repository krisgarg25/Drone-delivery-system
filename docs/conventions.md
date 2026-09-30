# Engineering conventions

Read this before changing any file in this repository. It is the contract between
engineers and between automated agents. The authoritative design sources are
`technical-build/*.md` in the parent directory; this file only records the rules
that must hold in code.

## 1. What this product is

A drone delivery **operator** platform. It turns a delivery request into an
energy-feasible aircraft assignment, a 4D route reservation, an operational record
and a monitoring workflow. Everything through Phase 4 is a **simulator**. It does
not command aircraft, and no simulated result is an aviation approval.

## 2. The three boundaries that never move

1. No module can command an aircraft. There is no command, mission-upload,
   parameter or firmware code path anywhere in the repository, not even behind a
   flag.
2. Lost or stale telemetry means **uncertainty**, never clear airspace. The last
   protected volume stays reserved until reconciled.
3. Onboard geofences and failsafes remain primary. This software proposes, records
   and explains; it is never the last line of defence.

Enforcement is structural: `import-linter` contracts, an OpenAPI no-command test,
and build-artifact checks. A boundary must not be bypassable by an environment
variable.

## 3. Synthetic data honesty

Every synthetic object carries provenance. Required fields:

- `authoritative: false` on synthetic entities, hard to strip;
- `altitude_reference` on every altitude, one of `AGL`, `AMSL`, `GROUND`, `PAD`,
  `UNSPECIFIED`;
- `source`, `retrieved_at`, `expires_at`, `freshness` on anything externally
  derived;
- `synthetic: true` and `requires_survey: true` on every generated obstacle height
  (Chandigarh OSM has essentially no vertical data);
- separation and energy numbers are **experiment parameters**, never certified
  values, and are read from a versioned config, never hard-coded.

Never describe a synthetic corridor as safe. Never label a candidate node with a
real hospital or business name.

## 4. Determinism rules for the simulation kernel

A run is identified by `bundle_sha256`, `engine_version`, `scenario_sha256`,
`policy_version`, `config_release_sha256`, `seed`, `python_version`,
`machine_architecture`. Inside the kernel:

- integer microseconds, never wall-clock time;
- one scheduler thread, no background threads;
- never iterate an unsorted dict or set to produce output or affect state;
- separate, seeded random streams for demand, wind, battery noise and faults;
- fixed timezone and locale;
- solver worker count is exactly one;
- no network, no filesystem writes outside the run directory, no production
  secrets, no live data source. External feeds are replayed from the frozen bundle.

Non-determinism is a defect and fails the CI replay gate.

## 5. Transactional authority

PostgreSQL is the system of record. The planner proposes; only the reservation
ledger commits. Within one transaction:

1. validate duplicate identity,
2. freeze the world snapshot,
3. rank eligible aircraft deterministically,
4. generate candidate routes,
5. reject restricted or unavailable candidates,
6. apply energy and recovery checks,
7. acquire row locks in canonical resource order,
8. recheck committed reservations,
9. insert mission, plan, reservations and decision record,
10. append the outbox event,
11. commit or roll back everything together.

Committed operational records are append-only. A correction is a superseding
record, never an in-place edit. The application role has `INSERT` and `SELECT` on
the audit log, never `UPDATE`, `DELETE` or `TRUNCATE`.

## 6. Planner and checker are separate implementations

The conflict checker is adversarial and deliberately more conservative. It reads
only committed records and raw geometry, sweeps continuous movement rather than
trusting planner buckets, and returns `SAFE | CONFLICT | UNPROVEN`. `UNPROVEN`
blocks release. It must not import `droneops_planner` or `droneops_geom_4d`. When
planner and checker disagree, that scenario is a release blocker, not a warning.

## 7. Package ownership

| Package | Owns | Must never own |
|---|---|---|
| `contracts` | versioned types, events, schemas, errors | database, frameworks |
| `core_domain` | entities and invariants | persistence, HTTP |
| `geom_core` | intervals, safe intervals, cells, references | Postgres, SDKs |
| `geom_4d` | swept volume and intersection | planner |
| `corridor_graph` | lane graph, candidate paths | live vehicle API |
| `airspace` | restriction validity | order pricing |
| `reservations` | commit state, atomic resources | UI |
| `planner` | candidate plan pipeline | checker internals |
| `dispatcher` | assignment, priority, fairness | vehicle commands |
| `energy` | energy profile, recovery | database |
| `facilities` | pads, chargers, crew windows | telemetry truth |
| `telemetry` | observation ordering, staleness | arbitrary command |
| `conflict_checker` | post-commit validation | planner, geom-4d |
| `persistence` | SQLAlchemy models, migrations | domain purity leakage |
| `platform` | clocks, IDs, config release, auth port | business policy |
| `observability` | tracing, logging, metrics | reservation mutation |
| `testing_support` | factories, seeded events, strategies | production secrets |

Adapters (`adapters/*`) are the only code that touches the outside world, and they
can never decide a reservation. `apps/sim_runner` never imports
`droneops_persistence` or any vehicle adapter. `apps/api` never imports
`droneops_gateway`. `apps/console` contains no scheduling logic.

## 8. Time, tenancy and identity

- All timestamps UTC, stored `timestamptz(6)`.
- `measurement_time` and `received_time` are separate fields. A late or reordered
  observation never overwrites a newer accepted measurement.
- Every business row is tenant-scoped by a non-null `tenant_id` and protected by
  row-level security.
- Every mission has a client-supplied idempotency key; a retry returns the same
  mission and cannot create a second commitment or charge.
- Every plan and decision records the `config_release_sha`.

## 9. State machines

Mission: `draft -> eligible -> planning -> reserved_pending_commit -> accepted ->
activated -> in_progress -> complete`, with `cancelled | failed | unresolved`.
`planning -> activated` is impossible; a commit must pass through the ledger.

Reservation: `tentative -> committed -> activated -> completed | cancelled |
unresolved`. `unresolved` exists precisely because an uncertain outcome does not
mean the airspace became empty.

Aircraft: `registered -> available -> reserved -> loading -> flying -> unloading
-> charging -> available`, plus `maintenance`, `offline`, `retired`, `contingency`.
Connectivity is orthogonal: online does not mean available.

## 10. Code style

- Python 3.12+, `from __future__ import annotations`, full type hints, `mypy
  --strict` on domain packages.
- No `Any` in planner, energy, reservation or contract APIs except at explicitly
  marked adapter boundaries.
- Frozen dataclasses or Pydantic models with `model_config = ConfigDict(frozen=True)`
  for value objects and immutable plans.
- No bare `except`. Never swallow an exception around a database commit.
- No random value without a recorded seed.
- Decimal or integer arithmetic for anything monetary or energy-critical;
  documented tolerance where float is unavoidable.
- Comments only for a non-obvious constraint or invariant. Never narrate what the
  code does.
- Ruff for lint and format; ESLint and Prettier plus TypeScript strict mode for
  the console.

## 11. Testing

Tests prove invariants, not functions. Every safety-critical scenario has a human
description, a machine-readable input, the expected invariant, expected metrics,
and a **what this does not prove** field.

- Unit: pytest. Property: Hypothesis. Concurrency: real PostgreSQL in Docker,
  never SQLite mocks. Contract: Schemathesis plus OpenAPI diff. Golden: versioned
  scenarios with expected metric vectors. E2E: Playwright.
- The invariant suite is `I1`-`I10` in `technical-build/06-testing-and-safety.md`.
- Rejection paths are first-class results. Metrics must show rejected and unfinished
  work, not only successes.

## 12. Commands

```bash
make bootstrap   # install workspace, hooks; never downloads aircraft or creates cloud resources
make db-up       # Docker PostGIS + Valkey
make migrate     # alembic upgrade head
make test        # pytest
make imports     # import-linter contracts
make lint        # ruff + mypy
make sim         # deterministic scenario run
make replay      # reproducibility check against golden
make console     # Vite dev server
```

## 13. Scope discipline

Through Phase 4 there is no marketplace, mobile app, customer billing, live
weather, UTM standard integration, vehicle command, Kubernetes, extra databases,
machine learning or 3D city visual effects. Adding one requires a written
justification that the reservation ledger and energy-aware dispatch are already
correct.

## 14. Every screen, report and export says

```
Simulation only. Not authorised for flight.
```
