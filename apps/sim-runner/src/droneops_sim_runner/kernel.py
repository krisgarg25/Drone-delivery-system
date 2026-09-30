"""Deterministic Phase 1 run assembly.

The heavy lifting lives in `droneops_sim_core.engine` (pure, stdlib-only). This module
turns a frozen bundle into a `WorldSpec`, executes one deterministic run, records the
full replay identity (seed + scenario + config release + engine version + python +
architecture) and renders the terminal report the operations team reads before any UI
exists. No wall clock, no network, no writes outside the run directory.
"""

from __future__ import annotations

import json
import platform
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from droneops_sim_core import Engine
from droneops_sim_core.model import AircraftConfig, RunMetric, WorldSnapshot
from droneops_sim_runner.bundle import ScenarioBundle
from droneops_sim_runner.hashing import config_release_sha256
from droneops_sim_runner.world import build_spec

ENGINE_VERSION = "0.2.0"
MICROSECONDS = 1_000_000
MINUTE_US = 60 * MICROSECONDS


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
    demand_family: str = "routine"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RunResult:
    identity: RunIdentity
    events: list[dict[str, Any]] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    metric_rows: tuple[RunMetric, ...] = ()
    snapshots: tuple[WorldSnapshot, ...] = ()


def _repo_root(start: Path) -> Path:
    """Nearest ancestor that holds both `configs` and `contracts`, i.e. the monorepo root."""
    for candidate in (start, *start.parents):
        if (candidate / "configs").is_dir() and (candidate / "contracts").is_dir():
            return candidate
    raise RuntimeError("no configuration release root found above the simulation runner")


def identity_for(
    bundle: ScenarioBundle, scenario_sha256: str, seed: int | None = None, family: str | None = None
) -> RunIdentity:
    repo_root = _repo_root(Path(__file__).resolve().parent)
    runtime = bundle.manifest["runtime"]
    return RunIdentity(
        scenario_id=bundle.scenario_id,
        bundle_sha256=bundle.bundle_sha256,
        engine_version=ENGINE_VERSION,
        scenario_sha256=scenario_sha256,
        policy_version=bundle.policy_version,
        config_release_sha256=config_release_sha256(repo_root),
        seed=seed if seed is not None else bundle.seed,
        python_version=platform.python_version(),
        machine_architecture=platform.machine(),
        demand_family=family or str(runtime.get("demand_family", "routine")),
    )


def load_aircraft(bundle: ScenarioBundle) -> tuple[AircraftConfig, ...]:
    documents = dict(bundle.documents)
    documents["runtime"] = bundle.manifest["runtime"]
    spec = build_spec(
        documents,
        bundle.scenario_id,
        bundle.seed,
        int(bundle.manifest["runtime"]["horizon_minutes"]),
        "routine",
    )
    return spec.aircraft


def run_scenario(
    bundle: ScenarioBundle, scenario_sha256: str, seed: int | None = None, family: str | None = None
) -> RunResult:
    """Execute a frozen bundle and return the ordered event log plus the metric vector."""
    runtime = bundle.manifest["runtime"]
    documents = dict(bundle.documents)
    documents["runtime"] = runtime
    spec = build_spec(
        documents,
        bundle.scenario_id,
        seed if seed is not None else bundle.seed,
        int(runtime["horizon_minutes"]),
        family or str(runtime.get("demand_family", "routine")),
    )
    result = Engine(spec).run()
    metrics = dict(result.metric_vector)
    metrics["demand_family"] = spec.orders[0].family if spec.orders else "routine"
    return RunResult(
        identity=identity_for(bundle, scenario_sha256, seed=seed, family=family),
        events=[dict(row) for row in result.events],
        metrics=metrics,
        metric_rows=result.metrics,
        snapshots=result.snapshots,
    )


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
    snapshot_rows = [row.as_dict() for row in result.snapshots]
    (directory / "world_snapshots.json").write_text(
        json.dumps(
            {"snapshot_count": len(snapshot_rows), "snapshots": snapshot_rows},
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    (directory / "REPORT.md").write_text(report_text(result), encoding="utf-8")


def _fmt_us(us: int) -> str:
    return f"{us / MINUTE_US:.2f} min"


def report_text(result: RunResult) -> str:
    identity = result.identity
    metrics = result.metrics
    title = f"{identity.scenario_id} ({identity.demand_family} demand, seed {identity.seed})"
    waits = [
        (label, metrics[mean_key], metrics[p95_key])
        for label, mean_key, p95_key in (
            ("pad wait", "mean_pad_wait_us", "p95_pad_wait_us"),
            ("charger wait", "mean_charger_wait_us", "p95_charger_wait_us"),
            ("mission duration", "mean_mission_duration_us", "p95_mission_duration_us"),
        )
    ]
    lines = [
        f"# Scenario {title}",
        "",
        "Simulation only. Not authorised for flight.",
        "",
        f"- engine_version: {identity.engine_version}",
        f"- bundle_sha256: {identity.bundle_sha256}",
        f"- scenario_sha256: {identity.scenario_sha256}",
        f"- config_release_sha256: {identity.config_release_sha256}",
        f"- policy_version: {identity.policy_version}",
        f"- python: {identity.python_version} / {identity.machine_architecture}",
        "",
        "## Terminal report",
        "",
        "| metric | value | unit | formula | denominator |",
        "|---|---|---|---|---|",
    ]
    for row in result.metric_rows:
        value = (
            row.value if not isinstance(row.value, dict) else json.dumps(row.value, sort_keys=True)
        )
        lines.append(
            f"| {row.metric_id} | {value} | {row.unit} | {row.formula} | {row.denominator} |"
        )
    lines += [
        "",
        "## Rejected work (shown, not hidden)",
        "",
        f"- rejected: {metrics['orders_rejected']}"
        f" (duplicates: {metrics['orders_duplicate_rejected']})",
        f"- unfinished at cutoff: {metrics['orders_unfinished_at_cutoff']}",
        f"- failed in flight: {metrics['missions_failed']}",
        "- reason vector: "
        + ", ".join(
            f"{key}={value}" for key, value in sorted(metrics["rejection_reasons"].items())
        ),
        "",
        "## Queue pressure",
        "",
        f"- repositioning: {metrics['repositioning_missions']} empty legs,"
        f" {metrics['repositioning_energy_wh']} Wh",
        "- telemetry loss events: "
        f"{metrics['telemetry_lost']} (airspace held as uncertainty, never released)",
        "",
        "## Illustrative economics (unvalidated model - never a price quote)",
        "",
        f"- cost per simulated delivery: {metrics['illustrative_cost_per_delivery_paise']}"
        " paise - ILLUSTRATIVE",
    ]
    for label, mean_us, p95_us in waits:
        lines.append(f"- mean {label}: {_fmt_us(mean_us)}; p95: {_fmt_us(p95_us)}")
    sensitivity = metrics["illustrative_cost_sensitivity_paise"]
    lines += ["- sensitivity to deliveries per hub per day:"]
    lines += [
        f"  - {key.replace('_', ' ')}: {value} paise" for key, value in sorted(sensitivity.items())
    ]
    lines += [
        "",
        "## What this does not prove",
        "",
        "- No real airspace, aircraft, weather or regulatory source was consulted.",
        "- Geometry, altitudes, capacities, batteries and demand are synthetic with provenance.",
        "- Separation and energy numbers are experiment parameters, not certified values.",
        "- A clean run is not an aviation approval and not a reservation commitment.",
        "",
    ]
    return "\n".join(lines)


def batch_summary_row(seed: int, metrics: dict[str, Any]) -> dict[str, Any]:
    return {
        "seed": seed,
        "submitted": metrics["orders_submitted"],
        "accepted": metrics["missions_accepted"],
        "completed": metrics["missions_completed"],
        "late": metrics["missions_late"],
        "failed": metrics["missions_failed"],
        "rejected": metrics["orders_rejected"],
        "unserved": metrics["unserved"],
        "energy_wh": metrics["energy_total_wh"],
        "wh_per_completion": metrics["energy_per_completion_wh"],
        "p95_duration_min_us": metrics["p95_mission_duration_us"],
        "p95_pad_wait_us": metrics["p95_pad_wait_us"],
        "reposition_wh": metrics["repositioning_energy_wh"],
        "cost_paise_per_delivery": metrics["illustrative_cost_per_delivery_paise"],
        "invalid_committed_overlaps": metrics["invalid_committed_overlaps"],
    }
