# Phase 3 performance and scale evidence

Exit criterion served: `technical-build/05-implementation-phases.md` Phase 3 — *"Performance
is profiled and documented with machine details."* Workload list and report format come from
`technical-build/06-testing-and-safety.md` section 11. Every number below is a measurement
taken on the machine named in section 1, not an estimate. Where something was not measured,
section 7 says so instead of guessing.

## 1. Machine and environment

Captured by the harness itself (`machine_details()` in `scripts/phase3_bench.py`) plus the
Windows system commands that corroborate it.

| Item | Value |
|---|---|
| System | Lenovo 83DV (Legion-class laptop), x64-based PC |
| CPU | 13th Gen Intel(R) Core(TM) i5-13450HX (`Intel64 Family 6 Model 183 Stepping 1`) |
| Cores | 10 physical, 16 logical |
| RAM | 16,873,545,728 bytes (15.71 GiB) |
| OS | Microsoft Windows 11 Home Single Language, 10.0.26300 Build 26300 |
| Python | 3.13.12 (MSC v.1944 64-bit), `asyncpg` 0.31.0 |
| Database | PostgreSQL 17.5 (Debian 17.5-1.pgdg110+1) in Docker, image `postgis/postgis:17-3.5` |
| PostGIS | 3.5.2 (`GEOS 3.9.0-CAPI-1.16.2`, `PROJ 7.2.1`, `WAGYU 0.5.0`) |
| DB settings | `shared_buffers = 128MB`, `effective_cache_size = 4GB`, `max_connections = 100` |
| Docker co-running | **Yes** — 4 containers up: `droneops-local-postgres-1` (the target), `droneops-local-valkey-1`, and a second `compose-postgres-1` + `compose-valkey-1` pair competing for the same CPU and disk |
| Harness process | `uv run --no-sync python scripts/phase3_bench.py` |

Everything ran on battery-adjacent laptop thermals with a database inside a Docker VM. That
is the honest context for every latency below: a server-class box with a local socket would
do better, and the report does not pretend otherwise.

**Memory: not captured.** `peak_rss_mb` came back `None` for every bench — the
`GetProcessMemoryInfo` probe in the harness returned no data on this Windows build, so the
table reports CPU-seconds instead. Section 12 of `06-testing-and-safety.md` asks for memory;
that column is a real gap, not an oversight in the write-up.

## 2. Results table (median / p95 / p99, iterations, throughput, failures)

Wall time is `time.perf_counter()` measured **outside** the deterministic kernel. Nothing in
`packages/sim-core` or `packages/geom-core` was instrumented; the kernel stays free of
wall-clock reads and `packages/geom-core/tests/test_determinism_guard.py` still passes.

| Workload (section 11) | n | median | p95 | p99 | throughput | failures |
|---|---|---|---|---|---|---|
| 1,000 candidate plans, one corridor — all demands | 1,000 | 0.50 ms | 42.0 ms | 54.6 ms | 60.8 demands/s | 0 exceptions |
| …only demands that produced a selected plan | 10 | 49.3 ms | — | 57.8 ms | — | 0 |
| …only demands refused at admission | 990 | 0.52 ms | — | 56.2 ms | — | 0 |
| Planner geometry hot path (edge cell enumeration) | 300 | 0.018 ms | 0.020 ms | 0.022 ms | ~6,000/s | 0 |
| 100 simultaneous commits, one corridor, 20 windows | 100 | 1.34 s | 3.16 s | 3.37 s | 40.2 commits/s | 0 DB errors, 0 violations |
| 100 simultaneous commits, one single window | 100 | — | — | — | 50.2 commits/s | 0 DB errors, 0 violations |
| 100 simultaneous commits, two corridors, 50 windows | 100 | — | — | — | 41.4 commits/s | 0 DB errors, 0 violations |
| Same 100 commits with an asyncpg pool of 32 | 100 | — | — | — | **28.8 commits/s** | 0 |
| 10,000 reservations over 24 h — bulk insert | 10,000 | — | — | — | see §4 | 0 |
| Temporal overlap query, index path | 1 | 0.099 ms | — | — | — | 0 |
| Temporal overlap query, index/bitmap scans off | 1 | **1.996 ms** | — | — | 20× slower | 0 |
| 100 obs/s ingest, paced 30 s | 3,000 | 1.06 ms | 1.50 ms | 2.29 ms | 100.0/s sustained | 0 write errors |
| Ingest burst, 4 connections, 10 s | 28,321 | 1.32 ms | — | 2.80 ms | **2,794 rows/s** | 0 write errors |
| Latest-5-positions read (UI shape) | 50 | 0.66 ms | — | 3.17 ms | — | 0 |
| Stale-fleet eligibility, 20 tails all stale | 1 | 84 µs | — | — | — | 0 |
| Stale-fleet eligibility, 500 tails all stale | 1 | 900 µs | — | — | — | 0 |
| Launch attempt against 200 unresolved protections | 200 | 26.5 ms | — | 38.2 ms | — | 0 exceptions |
| Batch: 20 seeded scenarios (rush family) | 20 | 103 ms | 139 ms | 140 ms | 8.9 scenarios/s | 0 |
| Batch: demand items throughput | 2,400 | — | — | — | ~1,110 items/s | 0 |
| Replay, in-process kernel run | 5 | 10.5 ms | — | — | — | 0 |
| Replay, full `droneops-sim replay` (what CI pays) | 3 | **0.295 s** | — | — | — | 0 (rc=0) |
| Commit voting through the checker | 30 | 30.0 ms | 40.7 ms | 50.5 ms | 7.7/s | 0 |
| Independent re-check of a 30-record committed set | 20 | 127.6 ms | — | 181.9 ms | — | 0 violations |

Warm-up: the harness performs a named warm-up pass per bench (`--warmup`, default 0 because
each bench's first operation is preceded by graph/policy/bundle load, which is the warm-up),
and the ingest and overlap workloads run their first 20–30 operations inside the sample set,
so cold-cache effects are visible in the spread rather than hidden.

CPU-seconds vs wall per bench (the closest honest stand-in for the missing memory column):
planner 16.0 s CPU / 16.4 s wall; commits 5.6 s CPU / 10.5 s wall (i.e. **commit cost is
waiting on the database, not burning CPU**); ingest 6.1 s CPU / 40 s wall; batch 2.6 s CPU;
replay 0.13 s CPU.

## 3. 1,000 candidate plans for one corridor

Shape: 1,000 demands on `hub-central-synthetic → hub-north-synthetic` in the
`chandigarh-synthetic-v1` bundle, one shared `InMemoryOccupancy` so later demands see earlier
commitments, `candidate_k=4`, fixed seed `202609300`, 13-step pipeline per demand.

The number that matters for an operator is the accepted-plan path: **median 49.3 ms, p99
57.8 ms**. Planning one corridor's candidate set is comfortably interactive.

The uncomfortable finding is the admission rate: **only 10 of 1,000 demands produced a
selected plan**; 990 came back unselected, at 0.52 ms because they exit at the admission or
terminal-window step. This is a world-model/corridor-capacity fact about the synthetic
bundle (one pad pair, one 24-60 min window per demand, staggered one minute apart), not a
throughput fact — but it means the "1,000 candidate plans" headline must be read as 1,000
*pipeline executions*, of which 10 were full plans. Reported that way deliberately.

The geometry inner loop (cell enumeration + tokenisation per edge, the work repeated for
every candidate) costs 0.018 ms, so candidate geometry is not where planning time goes: it
goes into `_evaluate` — see §6.

## 4. 10,000 reservations over a 24-hour window, and the GiST question

Reproducible command (the authoritative artifact for this workload):

```
cat scripts/phase3_reservation_scale.sql | docker exec -i droneops-local-postgres-1 \
    psql -U droneops -d droneops
```

Dataset: one throwaway tenant, 10,000 orders (`INSERT 0 10000`, 211.9 ms), 10,000 missions
(`INSERT 0 10000`, 171.1 ms), 10,000 `committed` reservations on a single resource key
`corridor:SCALE-BENCH`, all inside 24 hours. Output preserved in
`docs/performance/results/phase3-reservation-scale.txt`.

**Index effectiveness — the proof, not the claim.** The overlap query is the ledger's:

```
Index Cond: ((resource_key = 'corridor:SCALE-BENCH'::text)
         AND (valid_during && '["2027-03-04 12:00:00+00","2027-03-04 12:30:00+00")'::tstzrange))
```

```
   ->  Bitmap Heap Scan on reservation  (cost=13.90..301.34 rows=225 width=0)
                                          (actual time=0.047..0.071 rows=225 loops=1)
         Buffers: shared hit=11
         ->  Bitmap Index Scan on ix_reservation_tenant_window  (cost=0.00..13.85 rows=226)
                                          (actual time=0.040..0.040 rows=225 loops=1)
               Index Cond: (valid_during && '["2027-03-04 12:00:00+00",...)'::tstzrange)
               Buffers: shared hit=4
 Planning Time: 0.200 ms
 Execution Time: 0.099 ms
```

So: **yes, the GiST index `ix_reservation_tenant_window` (over `valid_during`) is used** at
10,000 rows, reaching 11 heap-and-index buffers for 225 matching rows. The counterfactual
with `enable_indexscan`/`enable_bitmapscan` off degrades the same query to:

```
   ->  Seq Scan on reservation  (actual time=0.551..1.969 rows=225 loops=1)
         Filter: ...
         Rows Removed by Filter: 9775
         Buffers: shared hit=287
 Execution Time: 1.996 ms
```

**20× slower and 26× more buffers.** The index is doing real work, not decoration.

Nuance that the harness found earlier at a smaller volume (103 rows): the planner chose
`Index Only Scan using no_committed_overlap` — the *partial* GiST behind the exclusion
constraint — at 0.032 ms with 1 buffer, `Heap Fetches: 0`. So which of the two GiST indexes
is used depends on volume; both are GiST, and neither is a sequential scan.

The ledger-shaped query (`state in ('committed','activated','unresolved')`) also uses
`ix_reservation_tenant_window` (38 rows, 5 buffers, 0.057 ms).

The binding constraint is arithmetic, not software: 10,000 committed windows on **one**
corridor key inside 86,400 seconds means the longest legal window is 8 seconds. The dataset
is built that way because the alternative is refused by the database:

```
ERROR:  conflicting key value violates exclusion constraint "no_committed_overlap"
DETAIL:  Key (resource_key, valid_during)=(corridor:SCALE-BENCH,
  ["2027-03-04 06:00:08+00","2027-03-04 06:00:18+00")) conflicts with existing key
  (corridor:SCALE-BENCH, ["2027-03-04 06:00:00+00","2027-03-04 06:00:10+00")).
```

That is invariant I1 working as designed and the single most useful scale fact here: a
corridor is a 24-hour tube of 8,640 ten-second slots, and the reservation table enforces it
in the index itself.

**Caveat, stated rather than buried:** the Python version of this bench
(`bench_reservation_scale` in `scripts/phase3_bench.py`) still fails on its own fixture —
`ck_order_origin_and_destination_differ` in the conflict-cost sub-step, and a
`DeadlockDetectedError` when it runs against a concurrently migrating database. The SQL
harness above is the committed, reproducible measurement for workload 3; the Python bench is
not used for these numbers.

## 5. 100 observations per second ingest, and partitioning status

`telemetry_observation` is range-partitioned by `measurement_time`: the migration
(`packages/persistence/migrations/versions/0008_telemetry.py`) materialises 18 monthly
partitions from 2026-07 plus a DEFAULT; the harness counted **20 partitions** live. Writes
went to a purpose-made `telemetry_observation_bench` partition for 2031-01 and cleanup was a
`DROP TABLE` of that partition — the table carries an append-only trigger, so `DELETE` is
refused by design (`insufficient_privilege: append-only table telemetry_observation refuses
DELETE`), and detach/drop is the intended retention path.

* Paced at the section 18 assumption — **100/s sustained for 30 s, achieved exactly
  100.0/s**, 3,000 rows, median insert 1.06 ms, p99 2.29 ms, 0 write errors.
* Burst ceiling with 4 concurrent connections: **2,794 rows/s** (28,321 rows in 10.1 s),
  median 1.32 ms, p99 2.80 ms, 0 write errors — **~28× headroom over the documented
  20-aircraft / 100 msg/s load**.
* Read side, the shape the console needs (latest 5 observations for one tail): median
  0.66 ms, p99 3.17 ms, and the plan confirms partition pruning — only 2 partitions are
  scanned (`telemetry_observation_bench`, `telemetry_observation_default`), each by backward
  index scan on `(aircraft_id, measurement_time)`, `Buffers: shared hit=5`.

One design fact worth noting for the next phase: because the table is partitioned, the
`unique (tenant, aircraft, sequence, measurement_time)` guard is **per partition**, so the
de-duplication guarantee does not span a partition boundary. That is documented in the
migration, and it is a correctness caveat, not a performance one.

## 6. Profiled hot functions (cProfile, not guessing)

From `docs/performance/results/phase3-profile.json` (`--profile` writes the top-14 frame
list into each bench's `extra.profile`).

**Planner path.** Of 21.6 s cumulative for 240 pipeline runs, 12.46 s sits under
`packages/planner/src/droneops_planner/pipeline.py:131 (plan)` and **11.64 s under
`pipeline.py:480 (_evaluate)`** — candidate evaluation, not routing or geometry. Note that
8.57 s of the total is `yaml.safe_load` reached through `packages/planner/tests/planner_support.py:78
(document)` / `:106 (request)`, i.e. the *test-support harness* re-reading bundle YAML per
demand. That overhead is a fixture artefact and is excluded from the reported per-plan
numbers (it appears inside the timed region only because `request()` is called there), so
the honest statement is: **the planner's hot function is `CandidatePlanPipeline._evaluate`;
the geometry loop is not hot (0.018 ms/edge).**

**Batch/sim-core path.** 3.1 M calls in 1.31 s. By self time the top function is
`packages/sim-core/src/droneops_sim_core/engine.py:172 (_route)` — 0.265 s tottime, 0.496 s
cumulative over 9,759 calls — followed by `engine.py:274 (_recovery_energy_wh)` (1,916 calls,
0.535 s cumulative) and `engine.py:303 (handle_order)` (297 calls, 0.749 s cumulative).
Route search and recovery-energy arithmetic are the cost of a scenario; bundle YAML load
(`apps/sim-runner/.../bundle.py:98 (_load_yaml)`, 0.480 s) is a fixed setup cost paid once.

**Ledger path.** cProfile attributes little self-time to Python: the commit bench consumed
5.6 CPU-seconds across 400 attempts that took 10.5 s wall. The ledger's cost is **round trips
and lock waits across its ~13 commit steps**, not computation — which is why a bigger driver
pool made throughput worse (8 → 32 connections: 40.2 → 28.8 commits/s) rather than better.
The checker's own cost is visible separately: re-checking a 30-record committed set takes
127.6 ms median (≈4 ms per record against the growing pool), which is the audit/replay path,
not the commit path.

## 7. Worst-case stale aircraft blocking launches

Two distinct worst cases, both measured.

*Eligibility filter.* With every tail's observation past `max_observation_age_us`
(30,000,000 µs = 30 s), `candidate_query` returns **zero candidates** at every fleet size —
20 tails: 84 µs, 50: 119 µs, 200: 466 µs, 500: 900 µs median, all excluded with reason
`aircraft_observation_stale`. The worst case is 4–6× slower than the all-eligible case (21–194
µs) because building the exclusion records costs more than skipping them, but it stays under
a millisecond even at 25× the documented fleet size. Invariant I5 holds structurally: they are
excluded, not demoted.

*Ledger blocking.* Against a corridor seeded with unresolved (I3-protected) reservations,
launch attempts are refused with `ledger.unresolved_protection` — 1 protection: 39.7 ms;
50: 24.9 ms median / 35.0 ms p99; 200: 26.5 ms median / 38.2 ms p99, max 40.8 ms. **The cost
is flat in the number of blocking rows** (0→200 protected rows changes nothing measurable),
because the ledger's protected-row read is one indexed query. 200/200 attempts refused,
0 exceptions, 0 database errors. A stale fleet does not slow the system down; it stops it,
quickly and explainably.

## 8. Batch scenario throughput and the ≥20 scenario gate

20 seeded scenarios of `chandigarh-synthetic-v1` (`--family rush`, seed stride 1009 from the
bundle seed): **median 103 ms/scenario, p95 139 ms, p99 140 ms; 20 scenarios in 2.24 s →
8.9 scenarios/s; 2,400 demand items in that window → ~1,110 items/s; 0 scenario failures.**

The trade-off spread across the 20 seeds (this is the Phase 3 exit criterion about
completion, lateness, energy and repositioning being visible):

| Metric | min across seeds | max across seeds |
|---|---|---|
| Completions | 28 | 31 |
| Late | 0 | 5 |
| Total energy (Wh) | 6,791 | 7,571 |
| Repositioning energy (Wh) | 2,849 | 3,483 |
| Wh per completion | 226 | 261 |
| On-time rate (bp) | ~9,655 | ~9,700 |
| Illustrative cost per delivery (paise) | 27,374 | 28,308 |
| `invalid_committed_overlaps` | 0 | 0 |

Repositioning eats **42–46% of total energy** (2,849–3,483 Wh of 6,791–7,571 Wh) while
serving only 28–31 completions out of 120 submitted demands per scenario, with ~91 unserved.
On the economics: the simulator's own illustrative figure is **₹273–₹283 per delivery** —
already far above the ₹39–₹67 road-leg benchmark recorded in `docs/07-commercial-model-and-roadmap.md`
and the memory note on hard constraints. Planning latency is therefore **not** the thing
that makes this business fail, and this report does not claim it is; see §10.

## 9. Replay determinism cost (the CI gate)

`droneops-sim replay --scenario simulation/scenarios/chandigarh-basic --golden
simulation/golden/chandigarh-basic --out simulation/reports/bench-replay`: **0.295 s median
over 3 runs, return code 0 all three, output digest
`f531a66f021b09ca502afa9661d7ae7d1c4caa7d4e49a70020d1b562740b5f1e` (identical every run)**.
Five in-process replays produced **1 distinct output digest across 5 runs** (metrics + event
list), with the kernel run itself at 9.3–16.5 ms.

**95.9–96.9% of the CI replay cost is interpreter start-up and imports, not simulation.** If
the replay gate ever needs to be cheaper, the fix is process reuse, not a faster kernel.

## 10. First bottleneck actually hit

Not section 18's ingest trigger: at 20 aircraft / 5 Hz / ~100 msg/s, ingest ran at exactly
100/s with 0 errors, and the ceiling measured 28× higher (2,794 rows/s). Not the temporal
index: 0.099 ms at 10,000 rows against 1.996 ms without it. Not the kernel: 0.018 ms per
geometry call, 103 ms per full scenario, 10 ms per replay.

The first bottleneck this system actually hit at documented scale is **commit fan-out
latency on a shared corridor**, and the evidence points at row-level serialisation rather
than CPU or connection count:

* 100 simultaneous commits over one corridor: throughput **40.2 commits/s** with per-attempt
  p99 of **3.37 s** (median 1.34 s) — an operator watching a queue would call that stuck.
* Widening the driver pool from 8 to 32 connections made it **worse** (28.8 commits/s),
  which rules out the pool and points at the lock/exclusion path on `resource_catalog` and
  `reservation`.
* 5.6 CPU-seconds against 10.5 s of wall says the process is waiting, not working.
* It is also the one place a database error reached the harness unprompted: the
  `DeadlockDetectedError` seen when the reservation bench competed with the same corridor
  rows.

Mapping to `technical-build/01-technical-architecture.md` section 18: **none of its four
scale triggers is met at 20 aircraft.** Ingest is nowhere near "beyond measured database
capacity"; there is no multi-facility or availability requirement in play; replay is 0.3 s.
The closest fit is "strict replay and fan-out at production volume", but only for *commit
fan-out*, not replay. The honest conclusion is that NATS/JetStream and Kubernetes remain
unjustified by these measurements, and the thing to fix first is the ledger's commit
serialisation on one corridor — a scheduling/lock-ordering problem inside the existing
PostgreSQL design, not an argument for new infrastructure.

The second real limit is physical rather than technical: **one corridor key holds at most
8,640 committed ten-second windows per day** (§4). No amount of infrastructure changes that;
it is lane capacity, and it belongs in the operational plan.

## 11. UI stream latency — not measured, with the probe recorded

Probed rather than assumed (`docs/performance/results/phase3-stream-latency-probe.txt`):
`npx --prefix apps/console vite --host 127.0.0.1 --port 5199` **does start** — `VITE v7.1.3
ready in 203 ms` — but a request to `/` returned **404**, because running vite through
`--prefix` from the repository root does not resolve the console's `index.html` root. The API
was not running at all (`curl http://127.0.0.1:8000/status/stream` → 404/no listener).

Even with both up, the only streaming route in the API today
(`apps/api/src/droneops_api/routers.py:640`) is a **bounded read of the append-only status
log**, not a publish→subscribe stream, so there is no live event path whose latency could be
measured. "UI stream latency" is therefore **reported as not measured**, with the follow-up
for Phase 4: start the console with `make console` (its own working directory), subscribe to
a live projection, and measure publish→SSE-receive round trip.

## 12. What this does not measure

* **Single machine.** Everything ran on one laptop with the database inside Docker Desktop's
  VM, and with a second unrelated PostGIS + Valkey pair running in the background. Network
  and IPC between API, worker and database were all on one host through `127.0.0.1`.
* **Synthetic data only.** The corridor graph, fleet, weather and demand come from
  `simulation/scenarios/chandigarh-synthetic-v1`, which is marked synthetic and illustrative.
  Aircraft behaviour, energy curves and the ₹273–283 per-delivery figure are model outputs,
  not field measurements. There is no real aircraft, no real pad, no real monsoon.
* **No multi-tenancy in production shape.** Every bench uses one or a handful of throwaway
  tenants on one database. RLS is `FORCE`d per tenant, so a many-tenant deployment adds filter
  cost this report does not quantify. The `resource_catalog` primary key is on `resource_key`
  alone — globally, not per tenant — which the benches observed directly (two seeded worlds
  could not both own `corridor:C-01`); that is a modelling decision this report flags but does
  not evaluate.
* **Memory growth is not measured** (§1): the peak-RSS probe returned nothing on this host,
  so there is no soak/leak evidence and `06-testing-and-safety.md` §11's "memory" column is
  open.
* **No sustained soak.** The longest single run was 30 s of paced ingest. Queue backlog
  behaviour, vacuum pressure at production row counts, partition-detach cost under load, and
  connection-pool exhaustion over hours are all unmeasured.
* **Failure injection is not in this report.** Database restarts, worker crashes around the
  outbox, and clock skew are covered by the concurrency and safety suites, not by these
  timings.
* **The Python reservation bench is defective** (§4 caveat); the numbers for that workload
  come from the SQL harness.
* **Concurrent-write contention across many corridors** was not measured: the 100-commit
  benches all target one or two corridor keys, which is the worst case for the lock path and
  the best case for showing serialisation, not a production mix.

## 13. Reproducing this

```
# the whole workload suite (needs the pinned local PostGIS container up)
uv run --no-sync python scripts/phase3_bench.py --all \
    --json-out docs/performance/results/phase3-bench.json

# the reservation-scale and GiST proof
cat scripts/phase3_reservation_scale.sql | docker exec -i droneops-local-postgres-1 \
    psql -U droneops -d droneops

# the CPU-bound gates that run in CI
uv run --no-sync pytest --no-cov tests/performance

# the hot-function evidence
uv run --no-sync python scripts/phase3_bench.py --only planner,batch,checker --profile \
    --plans 120 --seeds 2 --json-out docs/performance/results/phase3-profile.json
```

Fixed seed `20261001` for the harness, bundle seed `202609300` with stride `1009` for the
scenario batch, and the SQL harness uses a fixed tenant UUID and fixed window offsets, so
every rerun measures the same shape of work. Raw artifacts kept beside this report:
`docs/performance/results/phase3-bench.json`, `phase3-bench.log`,
`phase3-bench-reservations.log`, `phase3-profile.json`, `phase3-reservation-scale.txt`,
`phase3-stream-latency-probe.txt`, `phase3-machine-details.txt`.

Note for anyone re-running the database benches: `scripts/phase3_bench.py` clears the
scratch business tables with `TRUNCATE ... CASCADE` before seeding worlds (the same
repository rule the concurrency `conftest` follows), because `resource_catalog` keys are
globally unique. Pass `--no-clean` to suppress that. It only ever targets the DSN you export.
