# DroneOps Phase 0 repository skeleton

**Status:** Local Phase 0 skeleton only. This repository contains no aircraft command path and is not authorized for flight.

## Safety promise

- Phase 1 is simulation and control-plane support only.
- API routes create and inspect simulated missions; they cannot launch, land, return, upload or command aircraft.
- The gateway package is a read-only Phase 5 placeholder with no command directory.
- OpenAPI, dependency and architecture tests fail if a command path appears.
- Onboard aircraft fences and failsafes remain the primary safety layer.

## Quick start

```bash
uv sync --frozen --all-packages
uv run pytest
uv run lint-imports
uv run droneops-sim
```

The current Python workspace is compatible with Python 3.10+ for local use. Production CI should pin 3.12.

## Layout

```text
apps/           api, worker, sim-runner, gateway, console
packages/       pure domain, contracts, geometry, reservations, planner, checker
adapters/       weather, geodata, terrain, airspace import, future vehicle link
contracts/      OpenAPI, JSON Schema and event sources
configs/        versioned simulation and policy configuration
data/           scenario and golden test inputs
tests/          contract, property, concurrency and replay tests
adr/            architecture decisions
infra/          Compose, Terraform and observability definitions
```

## Start the simulated API locally

```bash
uv run uvicorn --app-dir apps/api/src droneops_api.main:app --reload
```

Then open `http://127.0.0.1:8000/docs` and confirm that only simulated planning routes are present.

## What is intentionally absent

- production or cloud database (a local PostGIS via `make db-up` holds synthetic
  simulation data only, and the Alembic baseline is not committed yet);
- live DigitalSky or UTM access;
- MAVLink command client;
- vehicle credentials;
- real Chandigarh route data;
- production Kubernetes configuration;
- flight authority.

## Before Phase 2

Create ADRs for geometry kernels, reservation commit, scenario bundle format and weather snapshots. Read `technical-build/` in the parent documentation repository for the approved architecture.

## Safety documents

- `security/threat-model.md` — the seven Phase 0 threats, each tied to the test, import contract or CI job that enforces it, plus what the model explicitly does not cover.
- `security/phase-1-boundaries.md` — asset classification and current boundary controls.
- `docs/phase-0-gate-report.md` — the Phase 0 exit gate walked criterion by criterion, with the commands actually run and their real output, including the gaps that remain open.
