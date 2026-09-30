# airspace

Restriction validity and route eligibility. Simulation only. **Not authorised for flight.**

## What it decides

| Question | Module | Answer |
| --- | --- | --- |
| Does this restriction apply right now? | `validity` | in force **and** fresh, or fail closed |
| Does this route or corridor edge meet it? | `eligibility` | `allowed_by_model`, `allowed_with_condition`, `blocked_now`, `blocked_during_window`, `no_launch_insufficient_data` |
| A new restriction just appeared - what changes? | `change` | unlaunched plans invalidated, airborne missions flagged, nothing deleted |
| What shape/volume/window is it? | `restriction`, `geometry` | six kinds, altitude band with an explicit reference, UTC validity window, mandatory provenance |

## Fail closed, in both directions

A restriction only constrains an operation when the claim is **in force** and the data
supporting it is **fresh**. Stale, expired, unsourced, clock-skewed or reference-mixed data
produces `no_launch_insufficient_data`, never a quiet "no restriction":

| Window | Data | Result |
| --- | --- | --- |
| active | fresh | applies - a red zone blocks, a yellow zone demands its condition |
| active | stale / expired / unknown / unsourced | **no launch** |
| ended | fresh | genuinely not in force (trustworthy data may say a zone lifted) |
| ended | stale | **no launch** - an ended window in stale data is unknown, not lifted |
| not yet | fresh | `blocked_during_window` with the exact interval: reschedule, not reroute |
| any | unusable | **no launch**, and an empty snapshot with no source behind it fails closed too |

`blocked_now` means the claim is in force at the decision instant *and* already covers the
first instant of the occupancy: there is no way to begin it. `blocked_during_window` carries
`blocked_from` / `blocked_until` so a planner can move around it.

## Airborne commitments are never removed

`evaluate_new_restriction` returns exactly one disposition per plan, and the types refuse the
dangerous combinations: an `AIRBORNE` plan cannot be constructed with `PLAN_INVALIDATED`, and
no `PlanImpact` can be built with `commitment_retained=False`. A closed mission is history and
is not re-evaluated. A stale new restriction is treated as possibly live: unlaunched invalidated,
airborne flagged.

## What is deliberately unavailable

* **No flight authorisation.** `NOT_A_CLEARANCE` travels on every result and
  `as_flight_authorisation()` raises. Red/yellow *names* imitate published conventions so the
  simulation reads legibly; resembling a standard is not Indian legal alignment.
* **No feed, no fetch.** DigitalSky/UTM data may enter only through `adapters/airspace-import`
  under authorised access. This package must never import it - the boundary is an AST test
  (`test_the_package_never_imports_the_import_adapter`) as well as an `import-linter` contract.
* **No authoritative restrictions in Phase 0.** `AUTHORITATIVE_SOURCES` is empty, so
  `authoritative=True` cannot be constructed by any caller.
* **No clock, no randomness, no I/O.** Every evaluation takes `now` and a `FreshnessPolicy`;
  the same inputs give the same verdict on replay.
* **No pricing, no dispatch, no reservation.** Ownership table, conventions section 7.

## Freshness numbers are experiment parameters

`FreshnessPolicy(max_trusted_age=6h, aging_grace=6h)` is illustrative and versioned
(`airspace-freshness-v1`), never a certified value. Unknown declared freshness is treated as
stale by default.

## Tests

```
uv run --no-sync pytest packages/airspace -q --no-cov
```

Geometry predicates are exact integer arithmetic on one tangent plane per comparison
(`geom-core`), and a query that cannot be projected raises instead of answering "no".
