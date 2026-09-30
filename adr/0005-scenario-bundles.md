# ADR 0005: Content-addressed scenario bundles

Status: proposed for Phase 1 implementation.

A simulation run will be defined by a scenario bundle: demand, fleet, corridor graph, restriction snapshot, weather, policies and faults. The bundle hash, engine version, policy version and seed form the reproducible run identity.

External data fetches happen in adapters and are frozen into the bundle. Deterministic runs must not make live HTTP calls.
