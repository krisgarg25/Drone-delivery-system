# ADR 0003: Postgres is the Phase 1 transactional boundary

Status: accepted.

Orders, missions, plan versions, reservations, decisions and audit events will live in PostgreSQL. PostGIS supplies spatial storage, and time-range exclusion constraints will provide the first hard guard against double-booked resources.

The application must still perform independent 4D geometry validation. The database is the atomic commit boundary, not the separation standard.
