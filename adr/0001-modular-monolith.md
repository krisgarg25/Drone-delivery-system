# ADR 0001: Modular monolith for Phase 1

Status: accepted.

We will begin with one Python monorepo containing a modular API, worker, simulation runner and read-only gateway placeholder. The reservation commit requires a single transactional boundary, so separate services would create distributed-consensus work without solving a demonstrated scaling problem.

Boundaries will be enforced by package interfaces and import-linter contracts. A gateway or spatial kernel may be split later only after measured need and an approved safety design.
