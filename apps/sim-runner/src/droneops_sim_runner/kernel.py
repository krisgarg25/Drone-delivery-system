"""Deterministic Phase 0 simulation kernel.

Rules this module obeys (docs/conventions.md section 4): integer microseconds, one thread,
no wall clock, no network, no writes outside the run directory, sorted iteration
everywhere, and one separately seeded stream per noise source. Identities are derived with
UUID5 from the frozen bundle so two runs of the same bundle emit the same identifiers.
Non-determinism here is a defect and fails the CI replay gate.
"""

from __future__ import annotations

import json
import platform
import random
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from droneops_conflict_checker import check_interval_overlap
from droneops_contracts import MissionStatus, PriorityClass, ReservationState
from droneops_core_domain import create_simulated_mission, transition_mission
from droneops_reservations import Reservation, canonical_resource_key, validate_commit_set
from droneops_sim_core import DeterministicEventQueue
from droneops_sim_runner.bundle import ScenarioBundle
from droneops_sim_runner.hashing import config_release_sha256

ENGINE_VERSION = "0.1.0"
MICROSECONDS = 1_000_000
STREAM_OFFSETS: dict[str, int] = {"demand": 0, "wind": 1_000_003, "battery": 2_000_069, "faults": 3_000_041}
FIXED_TIMEZONE = UTC
ID_NAMESPACE = uuid.UUID("00000000-0000-0000-0000-00000000d0ce")


@dataclass(frozen=True)
class RunIdentity:
    scenario_id: str
    bundle_sha256: str
    engine_version: str
    scenario_sha256: str
    policy_version: str
    config_release_sha256: str
    seed: int
    python_version: str
    machine_architecture: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "bundle_sha256": self.bundle_sha256,
            "engine_version": self.engine_version,
            "scenario_sha256": self.scenario_sha256,
            "policy_version": self.policy_version,
            "config_release_sha256": self.config_release_sha256,
            "seed": self.seed,
            "python_version": self.python_version,
            "machine_architecture": self.machine_architecture,
        }


@dataclass
class Aircraft:
    aircraft_id: str
    battery_wh: int
    cruise_speed_mps: int
    home_facility_id: str
    status: str = "available"


@dataclass
class RunResult:
    identity: RunIdentity
    events: list[dict[str, Any]] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)


def _minutes_to_us(minutes: int) -> int:
    return int(minutes) * 60 * MICROSECONDS


def _stable_id(kind: str, scenario_id: str, identifier: str) -> uuid.UUID:
    return uuid.uuid5(ID_NAMESPACE, f"{kind}|{scenario_id}|{identifier}")


def load_aircraft(bundle: ScenarioBundle) -> list[Aircraft]:
    fleet = bundle.document("fleet.yaml")
    rows = sorted(fleet.get("aircraft", []), key=lambda row: str(row["aircraft_id"]))
    aircraft = [
        Aircraft(
            aircraft_id=str(row["aircraft_id"]),
            battery_wh=int(row["battery_wh"]),
            cruise_speed_mps=int(row["cruise_speed_mps"]),
            home_facility_id=str(row["home_facility_id"]),
        )
        for row in rows
    ]
    if not aircraft:
        raise ValueError("fleet.yaml declares no aircraft")
    return aircraft


def _repo_root(start: Path) -> Path:
    """Nearest ancestor that holds both `configs` and `contracts`, i.e. the monorepo root."""
    for candidate in (start, *start.parents):
        if (candidate / "configs").is_dir() and (candidate / "contracts").is_dir():
            return candidate
    raise RuntimeError("no configuration release root found above the simulation runner")


def identity_for(bundle: ScenarioBundle, scenario_sha256: str) -> RunIdentity:
    repo_root = _repo_root(Path(__file__).resolve().parent)
    return RunIdentity(
        scenario_id=bundle.scenario_id,
        bundle_sha256=bundle.bundle_sha256,
        engine_version=ENGINE_VERSION,
        scenario_sha256=scenario_sha256,
        policy_version=bundle.policy_version,
        config_release_sha256=config_release_sha256(repo_root),
        seed=bundle.seed,
        python_version=platform.python_version(),
        machine_architecture=platform.machine(),
    )


def run_scenario(bundle: ScenarioBundle, scenario_sha256: str) -> RunResult:
    """Execute a frozen bundle and return the ordered event log plus the metric vector."""
    runtime = bundle.manifest["runtime"]
    policies = bundle.document("policies.yaml")
    demand = bundle.document("demand.yaml")
    corridor_doc = bundle.document("corridors.yaml")
    weather = bundle.document("weather.yaml")

    epoch = datetime.fromisoformat(str(policies["epoch"])).replace(tzinfo=FIXED_TIMEZONE)
    wh_per_100km = int(policies["energy_wh_per_100km"])
    reserve_fraction_bp = int(policies["energy_reserve_fraction_bp"])
    separation_buffer_us = int(policies["separation_buffer_us"])
    link_loss_bp = int(policies["link_loss_rate_bp"])
    headwind_bp = int(weather["headwind_bp"])
    corridor_length = {str(row["corridor_id"]): int(row["segment_m"]) for row in corridor_doc["corridors"]}
    aircraft = load_aircraft(bundle)

    streams = {name: random.Random(bundle.seed + offset) for name, offset in sorted(STREAM_OFFSETS.items())}
    queue = DeterministicEventQueue()
    ledger: list[Reservation] = []
    events: list[dict[str, Any]] = []
    completed: list[str] = []
    margins: list[int] = []
    counters = {
        "orders_requested": 0,
        "missions_accepted": 0,
        "completed_missions": 0,
        "rejected_no_aircraft": 0,
        "rejected_energy_margin": 0,
        "rejected_reservation_conflict": 0,
        "telemetry_lost": 0,
        "invalid_committed_overlaps": 0,
    }

    def emit(at_us: int, kind: str, **fields: Any) -> None:
        events.append({"at_us": at_us, "kind": kind, **fields})

    def free_aircraft() -> list[Aircraft]:
        return [row for row in aircraft if row.status == "available"]

    def on_complete(order_id: str, aircraft_id: str, at_us: int) -> None:
        for row in aircraft:
            if row.aircraft_id == aircraft_id:
                row.status = "available"
        completed.append(order_id)
        emit(at_us, "mission.complete", order_id=order_id, aircraft_id=aircraft_id)

    def on_link_loss(order_id: str, mission_id: str, at_us: int) -> None:
        counters["telemetry_lost"] += 1
        # Lost telemetry becomes uncertainty: the committed protected volume stays held
        # until it is reconciled. Uncertainty is never treated as clear airspace.
        emit(at_us, "telemetry.lost", order_id=order_id, mission_id=mission_id, state="protected_pending_reconciliation")

    def handle_order(order: dict[str, Any], arrival_us: int) -> None:
        counters["orders_requested"] += 1
        order_id = str(order["order_id"])
        corridor_id = str(order["corridor_id"])
        segment_m = corridor_length[corridor_id]
        jitter_us = streams["demand"].randrange(0, 31 * MICROSECONDS)
        at_us = arrival_us + jitter_us

        candidates = free_aircraft()
        if not candidates:
            counters["rejected_no_aircraft"] += 1
            emit(at_us, "order.rejected", order_id=order_id, reason="no_eligible_aircraft")
            return

        noise_wh = streams["battery"].randrange(0, 101)
        wind_wh = (segment_m * wh_per_100km * headwind_bp) // (100_000 * 10_000)
        base_wh = (segment_m * wh_per_100km) // 100_000
        consumed_wh = base_wh + wind_wh + noise_wh

        # Deterministic ranking: largest remaining margin first, identifier breaks ties.
        chosen = min(
            sorted(candidates, key=lambda row: row.aircraft_id),
            key=lambda row: (-(row.battery_wh - consumed_wh), row.aircraft_id),
        )
        reserve_wh = (chosen.battery_wh * reserve_fraction_bp) // 10_000
        energy_wh = consumed_wh
        if energy_wh + reserve_wh > chosen.battery_wh:
            counters["rejected_energy_margin"] += 1
            emit(at_us, "order.rejected", order_id=order_id, reason="energy_reserve_unmet")
            return

        margins.append(chosen.battery_wh - reserve_wh - energy_wh)
        cruise_us = (segment_m * MICROSECONDS) // chosen.cruise_speed_mps
        start_us = at_us + _minutes_to_us(2)
        end_us = start_us + cruise_us + separation_buffer_us

        mission = create_simulated_mission(
            mission_id=_stable_id("mission", bundle.scenario_id, order_id),
            order_id=_stable_id("order", bundle.scenario_id, order_id),
            origin_facility_id=str(order["origin_facility_id"]),
            destination_facility_id=str(order["destination_facility_id"]),
            priority=PriorityClass(str(order["priority_class"])),
            at=epoch + timedelta(microseconds=at_us),
        )
        transition_mission(mission, MissionStatus.ELIGIBLE)
        transition_mission(mission, MissionStatus.PLANNING)
        transition_mission(mission, MissionStatus.RESERVED_PENDING_COMMIT)

        reservation = Reservation(
            id=_stable_id("reservation", bundle.scenario_id, order_id),
            mission_id=mission.id,
            resource_key=canonical_resource_key("corridor", corridor_id),
            valid_during=(
                epoch + timedelta(microseconds=start_us),
                epoch + timedelta(microseconds=end_us),
            ),
            state=ReservationState.COMMITTED,
        )
        overlapping = [
            row
            for row in ledger
            if row.resource_key == reservation.resource_key
            and row.state in {ReservationState.COMMITTED, ReservationState.ACTIVATED}
        ]
        try:
            validate_commit_set([*overlapping, reservation], checker=check_interval_overlap)
        except ValueError:
            counters["rejected_reservation_conflict"] += 1
            emit(at_us, "order.rejected", order_id=order_id, reason="reservation_conflict")
            return

        ledger.append(reservation)
        transition_mission(mission, MissionStatus.ACCEPTED)
        chosen.status = "flying"
        counters["missions_accepted"] += 1
        emit(
            at_us,
            "mission.committed",
            order_id=order_id,
            mission_id=str(mission.id),
            aircraft_id=chosen.aircraft_id,
            corridor_id=corridor_id,
            energy_wh=energy_wh,
            start_us=start_us,
            end_us=end_us,
        )
        queue.schedule(end_us, "mission.complete", lambda at, o=order_id, a=chosen.aircraft_id: on_complete(o, a, at))
        if streams["faults"].randrange(0, 10_000) < link_loss_bp:
            queue.schedule(
                start_us + cruise_us // 2,
                "telemetry.lost",
                lambda at, o=order_id, m=str(mission.id): on_link_loss(o, m, at),
            )

    horizon_us = _minutes_to_us(int(runtime["horizon_minutes"]))
    orders = sorted(demand.get("orders", []), key=lambda row: (int(row["arrival_minute"]), str(row["order_id"])))
    for order in orders:
        arrival_us = _minutes_to_us(int(order["arrival_minute"]))
        if arrival_us > horizon_us:
            continue
        queue.schedule(
            arrival_us,
            "order.arrived",
            lambda _at, o=order: handle_order(o, _at),
        )

    queue.advance(horizon_us)
    counters["completed_missions"] = len(completed)

    invalid = 0
    for left in sorted(ledger, key=lambda row: str(row.id)):
        for right in sorted(ledger, key=lambda row: str(row.id)):
            if str(left.id) >= str(right.id) or left.resource_key != right.resource_key:
                continue
            if check_interval_overlap(left.valid_during, right.valid_during) == "CONFLICT":
                invalid += 1
    counters["invalid_committed_overlaps"] = invalid

    metrics: dict[str, Any] = {
        **counters,
        "committed_reservations": len(ledger),
        "energy_margin_min_wh": min(margins) if margins else 0,
        "energy_margin_mean_wh": (sum(margins) // len(margins)) if margins else 0,
        "events_emitted": len(events),
        "authorized_for_flight": False,
    }
    events.sort(key=lambda entry: (entry["at_us"], entry["kind"], str(entry.get("order_id", ""))))
    return RunResult(identity=identity_for(bundle, scenario_sha256), events=events, metrics=metrics)


def write_run(directory: Path, result: RunResult) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "events.jsonl").open("w", encoding="utf-8", newline="\n") as handle:
        for entry in result.events:
            handle.write(json.dumps(entry, sort_keys=True, separators=(",", ":")))
            handle.write("\n")
    (directory / "metrics.json").write_text(
        json.dumps(result.metrics, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (directory / "run.json").write_text(
        json.dumps(result.identity.as_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (directory / "REPORT.md").write_text(report_text(result), encoding="utf-8")


def report_text(result: RunResult) -> str:
    identity = result.identity
    lines = [
        f"# Scenario {identity.scenario_id}",
        "",
        "Simulation only. Not authorised for flight.",
        "",
        f"- engine_version: {identity.engine_version}",
        f"- bundle_sha256: {identity.bundle_sha256}",
        f"- scenario_sha256: {identity.scenario_sha256}",
        f"- config_release_sha256: {identity.config_release_sha256}",
        f"- policy_version: {identity.policy_version}",
        f"- seed: {identity.seed}",
        f"- python: {identity.python_version} / {identity.machine_architecture}",
        "",
        "## Metrics",
        "",
    ]
    lines += [f"- {key}: {value}" for key, value in sorted(result.metrics.items())]
    lines += [
        "",
        "## What this does not prove",
        "",
        "- No real airspace, aircraft, weather or regulatory source was consulted.",
        "- Separation and energy numbers are experiment parameters, not certified values.",
        "- A clean run is not an aviation approval and not a reservation commitment.",
        "",
    ]
    return "\n".join(lines)
