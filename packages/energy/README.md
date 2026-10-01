# packages/energy — energy and recovery feasibility (Phase 3)

The feasibility model behind "which aircraft can realistically fly this?". It
prices a **complete mission** and returns a **signed margin**, never a boolean:
dispatch has to rank plans, and the console has to show how close to the edge a
decision was.

Simulation only. Not authorised for flight.

## The terms

`assess_mission` charges five terms against one battery, in this order, so the
report can name which one broke (`MarginReport.breaking_term`):

| Term | Input it comes from | Charged as |
| --- | --- | --- |
| `outbound` | route distance, climb, descent, speed profile, wind, loaded mass | power × airtime per leg, plus potential energy of the climb, minus a tabulated fraction on descent |
| `handover` | hover / handover time | profile hover power × time, at loaded mass |
| `recovery` | recovery or alternate-site route | same legs, at **empty** mass: the payload was delivered |
| `reserve` | reserve requirement, alternate flag | fraction of the *usable* (aged) capacity, plus `alternate_fraction_bp` when recovering off-home |
| `gust_uplift` | peak gust over the mission window | extra reserve, capped by `gust_uplift_cap_bp` |

```text
estimated_consumption_wh = outbound + handover + recovery
reserve_required_wh      = reserve + gust_uplift
total_required_wh        = estimated_consumption_wh + reserve_required_wh
margin_wh                = available_wh - total_required_wh        # signed
```

**Invariant I4** is that sum: *outbound + handover + reserve + recovery or
alternate within available energy*. A mission whose delivery leg fits but whose
return does not is rejected on `recovery`; a mission with **no** recovery plan is
rejected outright (`RECOVERY_ROUTE_MISSING`) even though its margin looks large —
unproven energy is not free energy. See
`tests/test_invariant_I4_complete_mission.py` (CI-searchable as `test_i4_`).

## Wind, and the two ways it is wired

* **Sustained wind** is kinematic. Each leg solves a wind triangle at the
  commanded airspeed, so a headwind buys time and time buys power. The
  head/tailwind asymmetry is emergent, not tabulated, and wind reversal on one
  route gives a different margin for that reason.
* **Gusts** are a reserve uplift, never a consumption term: a peak gust is not a
  steady force a leg average can represent. Only the excess of peak over
  sustained counts, so a trace with no gust column produces exactly zero uplift.
  The coefficient is an **illustrative policy number**, not a measured handling
  quality.

Weather arrives only as a frozen `WindSnapshot` object with provenance
(`source`, `retrieved_at_us`, `expires_at_us`, `bundle_sha256`). The kernel takes
no client, no URL, no station id and no callable, so a deterministic run has
nothing it could fetch with; a stale snapshot is refused rather than read as calm
air. `tests/test_energy_purity_guard.py` scans this package's source for
`httpx`/`socket`/`time`/filesystem/wall-clock use and replays a workload under
five `PYTHONHASHSEED` values.

## Arithmetic and tolerance

Integer everywhere: millimetres, microseconds, deci-metres per second,
milliwatt-hours, basis points. Consumption and power round **up**, available
energy rounds **down**, so a reported margin is never optimistic. The only floats
are the sine and cosine of one angle per leg, quantised to basis points before any
energy is computed: **each wind component is uncertain by under 5e-5 of the wind
vector, which is worth well under one milliwatt-hour per leg** — three orders of
magnitude below the watt-hour resolution the model reports.

## Versioned, illustrative basis

Battery curves, mass effects, wind coefficients and degradation come from a named,
content-hashed `BatteryProfileVersion` (`airframe-x-sim@illustrative-1`), never
from a literal. `contracts/vehicle-profile/*.yaml` supplies mass, capacity, power
and reserve; **climb cost, descent recovery, wind coupling, degradation curves and
gust uplift are invented placeholders** listed in `PARAMETER_PROVENANCE` and
`ILLUSTRATIVE_ONLY_FIELDS`. Every output carries `disclaimer` and
`profile_sha256`, and a profile that drops `illustrative` must claim
`authoritative`, which this build refuses to do.

Because the numbers are placeholders, `sweep_sensitivity` re-runs the whole kernel
over a fixed perturbation grid (wind, gust, reversal, payload, route length,
handover, charge, state of health) and reports the margin shift and the **flip
delta** per knob: that is the trade-off visibility the Phase 3 exit criteria ask
for, and it tells a reviewer which coefficient to measure first.

## What this package does not do

No assignment, queueing, priority or fairness — `packages/dispatcher` owns those.
`assess_fleet` returns ranked margins with named rejection reasons and carries
`status` through untouched, so the I5 exclusion of maintenance and offline
aircraft stays in one place. No database (import-linter enforced). No aircraft
command of any kind.

**Battery model disagreement is not an acceptance.** `compare_battery_models`
prices the same mission on several profile versions; if they disagree on
feasibility, or spread beyond a versioned `ReviewPolicy` tolerance, the mission is
`review_required` and `accepted` is false (failure model, architecture section 19).

## Usage

```python
from droneops_energy import AIRFRAME_X_SIM, MissionEnergyRequest, Route, assess_mission, load_profile

profile = load_profile(AIRFRAME_X_SIM.name, AIRFRAME_X_SIM.version,
                       expected_sha256=AIRFRAME_X_SIM.parameters_sha256)
report = assess_mission(MissionEnergyRequest(
    mission_id="ord-001", payload_g=1000,
    outbound=Route.straight("outbound", 8_000_000, course_mdeg=90_000, climb_mm=60_000),
    handover=Route.idle("handover", hover_us=60_000_000),
    recovery=Route.straight("recovery", 8_000_000, course_mdeg=270_000),
    weather=frozen_snapshot, profile=profile, charge_bp=9_200,
))
print(report.summary)   # REJECTED margin=-37 Wh (available 368 Wh, ...) breaking term: recovery
```

```bash
uv run --no-sync pytest packages/energy -q --no-cov
uv run --no-sync mypy --strict packages/energy/src
```
