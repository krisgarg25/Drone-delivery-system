# Phase 0-4 build and gate report

**Date:** 1 October 2026
**Scope:** everything the build plan calls Phase 0 through Phase 4.
**Verdict up front:** Phases 0, 1, 2 and 3 meet their technical exit criteria with executed evidence. Phase 4 is built and browser-verified but **cannot be signed off**, because two of its five exit criteria require real human operators and no humans have tested it. Nothing here is an aviation approval, and the economics the simulation produced argue against the current business case rather than for it.

Read `docs/conventions.md` first; it states the rules this report measures against.

## Verification commands

```bash
make bootstrap && make db-up && make migrate
make test && make imports && make lint && make sim && make replay
npm --prefix apps/console run build
```

---

## Phase 0 — repository foundation and safety boundaries

**Met.**

| Exit criterion | Evidence |
|---|---|
| New developer can run bootstrap / test / dev | `make bootstrap`, `make test` green. `make dev` was **missing** for most of the build and has been added now: it brings up PostGIS, Valkey, API and console entirely in Docker. |
| CI fails if API imports gateway, or sim runner imports persistence secrets | 18 `import-linter` contracts, all KEPT, plus a CI canary job that plants a forbidden import and asserts the build fails. Verified by `tests/security/test_import_linter_bites.py`. |
| OpenAPI has no vehicle write command | `scripts/openapi_gate.py` reports 26 declared / 26 implemented / 0 undeclared, and `tests/contract/test_no_command_surface.py` walks every path, operationId and property against command vocabulary. |
| Repository private, backup documented, no live credentials | **Partially unmet.** Git history exists locally and is pushed to the authorised remote; branch protection, two-reviewer rules and a backup policy are documented in `technical-build/08` section 15 but not configured, and no remote settings were changed. |
| Configuration release manifest hashable | `scripts/config_release.py`, hashed into every plan, decision and event. |

Two real defects found and fixed during this phase: the pinned `gitleaks` hook referenced **v8.24.5, a release that does not exist**, so secret scanning had never actually run; and the CI canary left its planted violation in the tree, which would have failed the clean-tree assertion later in the same job.

## Phase 1 — Chandigarh synthetic world model

**Met.**

- `chandigarh-synthetic-v1`: 4 hubs, 6 nodes, 26 directed lanes, altitude bands A/B/C, two aircraft profiles with integer battery curves, 30 aircraft (12 available / 8 charging / 4 maintenance / 6 offline), routine-60 / rush-120 / failure-61 demand, frozen wind trace, injected faults.
- Deterministic replay is real, not claimed: two consecutive runs and a fresh process produce event-log sha256 `302107ed…9db6e2`, identical under `PYTHONHASHSEED` 0 / 4242 / 98765. Rush scenario: 28-31 of 120 completed, 6.8-7.6 kWh, ₹2.16-3.35 per delivery (illustrative).
- `tests/unit/test_module_layout.py` guards determinism structurally: forbidden imports, wall-clock reads and unseeded randomness are AST-scanned, and a hash-seed replay workload runs in a subprocess.
- **The vertical-data finding is confirmed and is worse than the September measurement.** A live query run during this build returned 27,750 buildings in the service box with **zero** tagged `height` and 739 with levels. `building_height_coverage: "0 of 27,750 tagged height"`. Every generated obstacle carries `obstacle_source: synthetic`, `vertical_authority: none`, `requires_survey: true`, `synthetic: true`, `authoritative: false`, and a lint test fails the build if any flag is missing.

One integrity defect worth knowing about: an Overpass mirror returned plausible geometry with **zero features**. Rejected bodies now go to a quarantine directory that replay cannot read, and a layer that the baseline proves is non-empty cannot be written empty. Without that guard, a bad mirror would have silently produced a "no obstacles" Chandigarh.

## Phase 2 — 4D protected volumes and reservation ledger

**Met, and this is the strongest part of the build.**

- **Continuous sweep, not frames.** Contact is decided over every integer microsecond by an exact rational quadratic solve — no sqrt, no epsilon, no float. The discretised sweep is inflated by `speed x dt` and is *proven to contain* the continuous one, in both directions. A retained test shows a 1 Hz sample saying SAFE where the sweep says CONFLICT; that contrast is the point.
- **The planner and the checker are genuinely independent.** The checker re-parses the policy itself, builds prisms on the pair's relative track with shapely, and holds head/tail pieces. `geom-4d` rasterises onto cells and decides in rational arithmetic. Different library, different construction, different arithmetic, and `droneops_geom_4d` is forbidden by contract.
- **A policy disagreement was found and resolved rather than hidden.** The checker padded every leg with timing + transition (15 s); the planner padded only manoeuvre legs. Those are different readings of the spec, and a planner that cannot satisfy its own checker blocks every release forever. Resolution: `timing_uncertainty` applies to every leg, `transition_buffer` only to manoeuvres, with `meaning_version: 2` recorded in every decision trace. A new 187-case cross-check proves agreement across gap, lateral and vertical separation while the implementations stay independent.
- **Adjacent lanes are convicted.** The checker admits committed records by proximity — shared key, adjacent lane, adjacent cell, overlapping volume — so two missions sharing no resource key can still convict each other. This matters because the database exclusion constraint physically cannot catch that case.
- **Concurrency, on real Postgres.** 100 simultaneous commits over one corridor: **20 committed, 80 refused with reason codes, 0 driver errors, 0 policy violations**. Twenty duplicates of one request produce one mission and one reservation. Lock inversion now genuinely contends — 34 samples, 15 waits, peak 2 waiters, one commitment, no escaped deadlock. An earlier version of that test recorded 0 deadlocks and passed; the real cause was a counting bug, and an idle test is worse than no test.
- **Uncertainty holds.** A stale or unresolved aircraft keeps its protected volume and blocks incompatible commitments. `UPDATE` on `audit_log` is refused by the database with `InsufficientPrivilege`; the app role holds `INSERT`/`SELECT` only; concurrent appends serialise on a per-tenant advisory lock without forking the chain.
- Nine golden bundles: the seven Phase 2 conflicts plus the frame-sampling contrast and an agreed allow-rule exception. Each carries the mandatory "what this does not prove".

Residual: `ledger` step 8's holder read is adjacency-expanded for lane and volume, but `ADJACENT_CELL` is only exercised where a grid is frozen, which the commit-path proofs do not always do.

## Phase 3 — aircraft, energy and facility dispatch

**Met.**

- **Eligibility excludes, it does not deprioritise.** Offline, maintenance, retired, contingency, airborne, busy, lost-link and stale-tail aircraft are absent from `candidate_ids`; a test asserts the energy layer's candidate set is byte-identical to the query's, so nothing re-enters downstream.
- **Two real bugs found here.** Hover energy was charged on a loop index it could never reach, so **hover silently vanished from every mission's energy sum**. The weather cross-check re-derived km/h and stamped it as dm/s, comparing mismatched units. Both fixed.
- Margin is a signed number with the breaking term named, so dispatch ranks plans instead of filtering booleans, and a battery-model disagreement blocks acceptance.
- Pads, chargers and crew are **unit resources**, so overbooking requires two claims on one identity and cannot occur. Outcomes are `SCHEDULED | QUEUED | REJECTED`; there is no overbook variant to reach.
- OR-Tools runs one search worker on a fixed seed; ranking digest is identical across `PYTHONHASHSEED` 0/1/7/12345. Starvation is bounded at a versioned 15 sim-minutes, tested at bound−1 µs. Priority cannot withdraw an airborne mission — enforced twice, because `core_domain` simply has no such transition, and the queue pins protected missions.
- Three routing strategies compared over **21 seeds** with identical demand, weather, fleet, seed, cutoff, profiles and capacity, guarded by `WeakenedBaselineError`: lane-based median 499 Wh (spread 448) with 3 unserved; direct and hybrid 697 Wh (spread 196) with 1 unserved. Cheaper per flight is not the same as serving the work, and neither baseline was weakened to reach that.

### Performance, profiled on the build machine

Lenovo 83DV, i5-13450HX 10C/16T, 16.9 GB RAM, Windows 11 10.0.26300, Python 3.13.12, PostgreSQL 17.5 + PostGIS 3.5.2 in Docker, 4 containers co-running.

| Workload | Median | p95 | p99 | Rate |
|---|---|---|---|---|
| 1,000 candidate plans, one corridor | 0.52 ms | 42.8 ms | 57.0 ms | 60.2/s |
| 400 concurrent commits | 1584 ms | 3180 ms | 3379 ms | 33-40/s |
| 100 obs/s telemetry ingest | 1.06 ms | 1.50 ms | 2.29 ms | sustained, burst 2,794/s |
| 20-seed batch (2,400 demands) | 112 ms | 147 ms | 152 ms | 8.9 scenarios/s |

The GiST overlap index is proven real: `Bitmap Index Scan`, 11 buffers, 0.099 ms versus a forced sequential scan at 1.996 ms — **20x**. Peak RSS could not be captured (psapi returned nothing) and multi-tenant soak was not measured.

**First bottleneck: commit fan-out serialisation at ~40 commits/s.** Widening the pool from 8 to 32 made throughput *worse* (28.8/s) — 5.6 CPU-seconds against 10.5 wall seconds. None of the architecture's section-18 scale triggers are met yet; ingest has ~28x headroom. There is also a hard structural limit worth knowing before anyone plans a corridor: **one corridor resource key holds at most 8,640 ten-second committed windows per day.**

## Phase 4 — operations console and decision explanation

**Built and browser-verified. Not signable.**

- 8 panels across 7 slots: mission queue, mission board, fleet, economics, decision trace, replay, comparison, exceptions. Nine map layers via MapLibre + deck.gl. Dark, token-based design system where planned / committed / observed / synthetic / uncertain / rejected each carry a **second non-hue channel**, so the distinction survives colour blindness.
- Typed entirely from the OpenAPI contract (`src/generated/api-types.ts`, generator verified); `tsc --noEmit`, `eslint`, `prettier` and the production build are clean, zero uncaught JS on every route.
- **Independently audited, and the audit disproved the builder.** The console engineer claimed the rejection path was 8 tab stops; measurement found **13**, and the refusal itself was an unfocusable heading. Axe reported `serious` `dlitem` violations the console's own "26/26 contrast pass" had never looked for, because that audit only checked contrast. After repair: **13 → 3 stops** to the refusal, 4 to its technical detail, axe violations 0 serious / 0 moderate, focus trapped and restored on Escape, `prefers-reduced-motion` actually computing `0s` (was `0.000001s`).
- No scheduling logic in the browser, verified against the **built bundle**, not just source: no non-GET methods, no command routes, no scheduler identifiers. No command affordance exists anywhere in the UI.
- "Simulation only. Not authorised for flight." in header and footer on every route, with the provenance strip carrying scenario id, source, retrieval date and hash.
- 14 Playwright-over-pytest and 10 native Playwright specs pass with a live API and preview server.

### Unmet Phase 4 exit criteria — stated plainly

1. **"Three internal users can complete a scenario walkthrough without explanation."** Not met. No human has used this. Software cannot certify itself here.
2. **"A test operator can find the cause of a rejection in under 60 seconds."** Not met. The path is now 4 tab stops to the technical detail, which is *mechanically* fast, but 60 seconds is a human comprehension measurement and nobody has been timed.
3. "UI fully typed from contracts" — **met.**
4. "No browser-only scheduling logic" — **met.**
5. "Accessibility checks pass for contrast, focus and keyboard" — **met** on axe, contrast, focus and keyboard. Touch targets below 44 px at 390 px width (9 found) remain, and no screen-reader announcement was verified by an actual AT.

### Still reference-level in the API

`reserve` and the simulation/comparison/decision reads go through the real ledger, corridor graph and dispatcher. **Fleet, facilities, observations/uncertainty, proposals, status and order intake still resolve through `reference.py`, which is explicitly labelled a test double.** No persistence read-model helpers were written; instead the composition accepts injected ports, because `droneops_api` is contractually forbidden from importing `droneops_persistence`. The default app is now the production composition, so this is a set of unwired ports, not a mock left in charge.

---

## What this build does not prove

Say these out loud in any demo:

- A conflict-free simulated schedule is **not** collision safety, and no separation number here is certified — they are experiment parameters in a versioned config.
- Chandigarh has **no** verified obstacle heights, no surveyed pads, no approved corridors, no airport-boundary verification and no DigitalSky access. Nothing in this repository is authorised to fly.
- The India constraints that bound the whole idea are unchanged by any of this code: **no notified BVLOS framework exists**, registration without type certificate is suspended, and winch and parachute drops are banned — which is why `LAND_TRANSFER` is the only permitted handover and `WINCH`/`PARACHUTE` are rejected at the type level.
- No aircraft, supplier, quote, customer or route has been contacted.

## The finding that matters most

The simulator's own numbers undercut the business thesis as currently configured. At the tested load the modelled cost is **11,500 INR per delivery**, roughly **217x** the illustrative 39-67 INR road leg. Third-party targets put viable cost per delivery under about 3 USD.

Two readings, both unwelcome and both honest. Either throughput is the whole problem — 8 orders per seed is nowhere near a loaded hub, and cost per delivery falls with density, which the sensitivity routine exists to show — or the lane-based strategy's cheaper energy (499 Wh versus 697 Wh) is the lever and it leaves work unserved. **The economics of this business are decided by utilisation per hub per day, and that is a demand and contracting question, not a software question.** The software now measures it; it cannot fix it. Every cost figure in this build remains illustrative and unvalidated.

## Recommended next actions

1. Run the 60-second rejection test and the three-user walkthrough with real people; that is the only thing standing between Phase 4 and sign-off.
2. Wire the remaining reference-plane ports (fleet, facilities, observations, proposals, status, order intake) to real read models.
3. Push the utilisation question: rerun the cost model at 50-200 deliveries per hub per day and find the density where the number stops being absurd, before spending on aircraft.
4. Configure branch protection and the two-reviewer rule for reservation, telemetry, auth, CI and contracts.
5. Then, and only then, Phase 5 — which is safety engineering, not a demo feature.
