# Domain overview

The Phase 1 domain treats an order as a commercial request, a mission as an accepted simulation job, a plan version as an immutable candidate and a reservation as the authoritative time-bounded resource commitment.

Critical invariants:

- a duplicate order key returns the same mission;
- an unlaunched plan is the only thing priority may resequence;
- lost telemetry produces uncertainty, never released airspace;
- no API route may command an aircraft;
- the independent conflict checker must validate committed reservations.
