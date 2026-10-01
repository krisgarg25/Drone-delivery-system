# facilities

Pads, chargers, loading and crew window scheduling (Phase 3). Site capacity is
modelled as unit resources addressed by canonical `geom-core` keys; the
turnaround chain (approach, landing, handover/transfer, loading, departure, plus
recharge on a separate resource) is placed against opening hours, declared
outages and crew duty windows.

This package **proposes**; only the reservation ledger commits. Capacity is
never overbooked (invariant I6) because a unit holds one claim at a time and a
short site produces a reason-coded queue entry or rejection instead. Handover is
`LAND_TRANSFER` only. It cannot import telemetry truth or the vehicle link
(`.importlinter`), owns no clock, and decides no priority, price or assignment -
alternate-site and repositioning costs are surfaced as data for the dispatcher.

```bash
uv run --no-sync pytest packages/facilities -q --no-cov
uv run --no-sync mypy --strict packages/facilities/src
```

Simulation only. Not authorised for flight.
