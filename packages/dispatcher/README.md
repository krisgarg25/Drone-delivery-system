# dispatcher

Aircraft assignment, priority and queue fairness. Phase 3 of the build plan; the
package that decides *which* aircraft can realistically fly a mission, not merely
which one is online.

## What it owns

| Concern | Module |
|---|---|
| Versioned policy: fairness bound, ranking weights, solver settings | `policy.py` |
| Candidate query - invariant I5, exclusion not demotion | `eligibility.py` |
| Energy screen (I4), pad/crew/charger waits, repositioning price | `ranking.py` |
| OR-Tools assignment and time windows, one worker, fixed seed | `solver.py` |
| Queue fairness, aging, starvation bound, resequencing | `queue.py` |
| Four kinds of priority with four different power sets | `priority.py` |
| Airborne change as a proposal an operator accepts (section 13) | `airborne.py` |
| Rejection explanations and the append-only decision record | `rejection.py` |
| The round, and the seeded three-way strategy comparison | `dispatch.py` |

## What it never does

* **No vehicle link.** `.importlinter` contract *Dispatcher cannot reach a vehicle
  link* forbids importing the MAVLink adapter or the gateway. An accepted airborne
  change is a recorded intent, never a command.
* **No commits.** The facility scheduler is asked to `propose`, never `commit`; the
  decision log is the dispatcher's own record, and the reservation ledger remains the
  only thing that commits (`conventions.md` section 5).
* **No re-derived physics.** Energy margins come from `droneops_energy`, capacity and
  waits from `droneops_facilities`, candidate routes from `droneops_planner`. The
  dispatcher ranks and chooses; it does not re-implement any of them.
* **No deleted commitments.** Priority may resequence unlaunched work only. Airborne
  and committed missions stay pinned in the queue, and `withdrawal_is_legal()` asks
  the mission state machine - which cannot express cancelling an in-progress mission.

## Determinism

`SolverSettings` accepts exactly one search worker and a fixed seed; CP-SAT with more
than one worker races a portfolio and returns a timing-dependent answer, which would
silently break replay (invariant I7). The tie-break is compiled into the objective so
the optimum is unique. `tests/test_phase3_exit_criteria.py` proves the ranking is
byte-identical across `PYTHONHASHSEED` values in fresh interpreters.

Starvation is bounded by a named, versioned number: `starvation_bound_us`
(default 15 simulated minutes). Aging can lift routine work to the head of the
ordinary band; it can never out-rank a verified life-critical mission, and a
commercial SLA can never buy an emergency right of way.

## Scenario comparison

`simulation/scenarios/dispatch-competition-v1/comparison.yaml` holds the demand, the
three routing strategies and the seed list; `ScenarioComparison` refuses to report a
comparison in which any arm was given different aircraft, capacity, energy profile,
cutoff or seed - a weakened baseline is misleading, not conservative.
`simulation/scenarios/dispatch-safety-v1/scenarios.yaml` holds the three safety
scenarios (`battery-shortfall`, `priority-arrival`, `priority-airborne-request`) with
their expected invariants and "what this does not prove".

## Commands

```bash
uv run --no-sync pytest packages/dispatcher -q --no-cov
uv run --no-sync mypy --strict packages/dispatcher/src
uv run --no-sync lint-imports
```

Simulation only. Not authorised for flight.
