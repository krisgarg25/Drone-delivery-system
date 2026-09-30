# DroneOps developer entry points.
#
# Every target is a plain command that also works in Git Bash on Windows. GNU make is
# required; on Windows use the Git Bash shell (mingw32-make ships with MSYS2/MinGW and
# works here). No target downloads aircraft data, contacts a flight service or creates
# any cloud resource. Phase 0 is a simulator: see docs/conventions.md section 2.

.DEFAULT_GOAL := help
PYTHON := uv run --no-sync

# Git Bash / MSYS on Windows resolves `bash` from PATH; Linux/macOS already have /bin/bash.
ifeq ($(OS),Windows_NT)
SHELL := bash
.SHELLFLAGS := -u -e -o pipefail -c
else
SHELL := /bin/bash
.SHELLFLAGS := -u -e -o pipefail -c
endif

COMPOSE_FILE := infra/compose/docker-compose.yml
COMPOSE := docker compose -f $(COMPOSE_FILE)
CONSOLE_DIR := apps/console
SIM_BUNDLE := simulation/scenarios/chandigarh-basic
SIM_WORLD := simulation/scenarios/chandigarh-synthetic-v1
GOLDEN_DIR := simulation/golden/chandigarh-basic
RUN_DIR := simulation/reports
LICE_ALLOWLIST := configs/licensing/licence-allowlist.txt

.PHONY: help bootstrap db-up db-down db-status migrate reset-db api worker test unit \
        imports lint format typecheck sim replay console check sec licence audit sbom \
        openapi-diff clean-hooks e2e-install dev-env sim-freeze sim-world sim-batch

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-14s %s\n", $$1, $$2}'

bootstrap: ## Install workspace deps from the lock, git hooks and console deps (no aircraft, no cloud)
	uv sync --frozen --all-packages
	$(PYTHON) pre-commit install --install-hooks -t pre-commit
	$(PYTHON) python scripts/check_boundaries.py --self
	$(MAKE) --no-print-directory console-install
	@echo "bootstrap complete. Next: make db-up && make migrate && make test"

db-up: ## Start local PostGIS + Valkey in Docker (local development only)
	$(COMPOSE) up -d --wait postgres valkey
	$(COMPOSE) ps

db-down: ## Stop the local data stack
	$(COMPOSE) down

db-status: ## Show container health
	$(COMPOSE) ps

db-clean: ## Stop the stack and delete the local Postgres volume (destructive, local only)
	$(COMPOSE) down -v

migrate: ## Apply Alembic migrations to head against the local PostGIS
	$(PYTHON) alembic -c packages/persistence/alembic.ini upgrade head

api: ## Run the API on :8000 (simulation only, no command route)
	$(PYTHON) uvicorn --app-dir apps/api/src droneops_api.main:app --host 127.0.0.1 --port 8000

worker: ## Run the background worker process
	$(PYTHON) droneops-worker

test: ## Full test suite (unit + property + contract; DB-backed tests skip without Postgres)
	$(PYTHON) pytest

unit: ## Unit tests only
	$(PYTHON) pytest -m "not concurrency and not e2e and not slow"

imports: ## import-linter architecture contracts
	$(PYTHON) lint-imports

lint: ## ruff lint (then format check)
	$(PYTHON) ruff check .
	$(PYTHON) ruff format --check .

format: ## ruff autofix + format
	$(PYTHON) ruff check . --fix
	$(PYTHON) ruff format .

typecheck: ## mypy --strict over the strict-typed packages
	$(PYTHON) mypy

sim: ## Deterministic scenario run from the frozen bundle
	mkdir -p $(RUN_DIR)
	$(PYTHON) droneops-sim run --scenario $(SIM_BUNDLE) --out $(RUN_DIR)/latest

replay: ## Re-run the scenario and prove byte-identical output against the golden
	mkdir -p $(RUN_DIR)
	$(PYTHON) droneops-sim replay --scenario $(SIM_BUNDLE) --golden $(GOLDEN_DIR) --out $(RUN_DIR)/replay

sim-freeze: ## Re-record bundle digests and promote the current run to the golden (deliberate)
	$(PYTHON) droneops-sim freeze --scenario $(SIM_BUNDLE) --write-manifest
	$(PYTHON) droneops-sim run --scenario $(SIM_BUNDLE) --out $(RUN_DIR)/latest
	$(PYTHON) droneops-sim freeze --scenario $(SIM_BUNDLE) --golden $(GOLDEN_DIR) --out $(RUN_DIR)/latest

sim-world: ## Deterministic run of the full chandigarh-synthetic-v1 world bundle
	mkdir -p $(RUN_DIR)
	$(PYTHON) droneops-sim run --scenario $(SIM_WORLD) --out $(RUN_DIR)/world

sim-batch: ## 20 seeded scenarios of the v1 world as a batch with the metrics table
	mkdir -p $(RUN_DIR)
	$(PYTHON) droneops-sim batch --scenario $(SIM_WORLD) --seeds 20 --family rush --out $(RUN_DIR)/batch-20

console-install: ## Install console node modules from its lockfile
	@if [ -f $(CONSOLE_DIR)/package-lock.json ]; then \
		npm --prefix $(CONSOLE_DIR) ci --no-audit --fund=false; \
	else \
		echo "no console lockfile yet; skipping node install"; \
	fi

console: ## Vite dev server for the operations console
	npm --prefix $(CONSOLE_DIR) run dev

openapi-diff: ## OpenAPI diff plus the no-command-route gate
	$(PYTHON) python scripts/openapi_gate.py --report data/reports/openapi-diff.json

licence: ## Licence allowlist check (MIT, BSD, Apache-2.0, ISC only; reviewed exceptions only)
	$(PYTHON) python scripts/check_licences.py \
		--allowlist $(LICE_ALLOWLIST) \
		--exceptions configs/licensing/licence-exceptions.txt \
		--report data/licenses/python-licences.json

audit: ## Known-vulnerability scan of the locked environment
	$(PYTHON) pip-audit --progress-spinner off

sbom: ## CycloneDX SBOM of the locked Python environment
	mkdir -p data/sbom
	uv export --frozen --all-packages --no-dev --no-emit-project -o data/sbom/requirements.txt
	$(PYTHON) cyclonedx-py requirements --output-format json -o data/sbom/droneops.cdx.json data/sbom/requirements.txt
	@echo "SBOM written to data/sbom/droneops.cdx.json"

sec: licence audit ## Run every security gate

check: lint typecheck imports test openapi-diff sim replay ## Everything a PR must pass

e2e-install: ## Download Playwright browsers (only needed for UI smoke tests)
	$(PYTHON) playwright install chromium

clean-hooks: ## Uninstall pre-commit hooks
	-$(PYTHON) pre-commit uninstall
