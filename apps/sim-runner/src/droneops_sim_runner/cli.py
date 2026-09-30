"""`sim run | replay | hash | freeze | compare` entry point.

The runner is a pure consumer of a frozen scenario bundle. It never opens a database
connection, never imports persistence and never reaches a vehicle link; those boundaries
are enforced by `.importlinter`, not by a flag.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from droneops_sim_runner.bundle import load_bundle
from droneops_sim_runner.hashing import bundle_sha256, file_sha256, manifest_sha256
from droneops_sim_runner.kernel import batch_summary_row, run_scenario, write_run

MANIFEST_NAME = "bundle.yaml"
REPLAY_ARTIFACTS = ("events.jsonl", "metrics.json")


def scenario_digest(directory: Path) -> str:
    scenario = directory / "scenario.yaml"
    if scenario.exists():
        return file_sha256(scenario)
    return bundle_sha256(directory)


def artifact_digest(directory: Path) -> str:
    entries = [{"path": name, "sha256": file_sha256(directory / name)} for name in REPLAY_ARTIFACTS]
    return manifest_sha256(entries)


@click.group()
def main() -> None:
    """Deterministic, simulation-only scenario runner."""


@main.command("run")
@click.option(
    "--scenario",
    "scenario_dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    required=True,
)
@click.option("--out", "out_dir", type=click.Path(file_okay=False, path_type=Path), required=True)
@click.option(
    "--seed", type=int, default=None, help="override the bundle seed (recorded in run.json)"
)
@click.option("--family", type=click.Choice(["routine", "rush", "failure"]), default=None)
def run_command(scenario_dir: Path, out_dir: Path, seed: int | None, family: str | None) -> None:
    """Execute a frozen bundle and write events, metrics and a report."""
    bundle = load_bundle(scenario_dir)
    result = run_scenario(bundle, scenario_digest(scenario_dir), seed=seed, family=family)
    write_run(out_dir, result)
    click.echo(f"run complete: {result.identity.scenario_id} -> {out_dir}")
    click.echo(f"bundle_sha256={result.identity.bundle_sha256}")
    click.echo(f"config_release_sha256={result.identity.config_release_sha256}")
    click.echo(json.dumps(result.metrics, indent=2, sort_keys=True))
    click.echo("Simulation only. Not authorised for flight.")


@main.command("replay")
@click.option(
    "--scenario",
    "scenario_dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    required=True,
)
@click.option(
    "--golden",
    "golden_dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    required=True,
)
@click.option("--out", "out_dir", type=click.Path(file_okay=False, path_type=Path), required=True)
def replay_command(scenario_dir: Path, golden_dir: Path, out_dir: Path) -> None:
    """Re-run a bundle and fail unless the output is byte-identical to the golden."""
    bundle = load_bundle(scenario_dir)
    write_run(out_dir, run_scenario(bundle, scenario_digest(scenario_dir)))
    actual = artifact_digest(out_dir)
    expected_path = golden_dir / "digest.txt"
    if not expected_path.exists():
        raise click.ClickException(f"{expected_path} missing; run `droneops-sim freeze --golden`")
    expected = expected_path.read_text(encoding="utf-8").strip()
    if actual != expected:
        click.echo(f"golden digest: {expected}", err=True)
        click.echo(f"replay digest: {actual}", err=True)
        raise click.ClickException(
            "replay is not reproducible. Non-determinism in the kernel is a defect, not a warning."
        )
    click.echo(f"replay reproducible: {actual}")


@main.command("freeze")
@click.option(
    "--scenario",
    "scenario_dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    required=True,
)
@click.option(
    "--golden", "golden_dir", type=click.Path(file_okay=False, path_type=Path), default=None
)
@click.option("--out", "out_dir", type=click.Path(file_okay=False, path_type=Path), default=None)
@click.option(
    "--write-manifest",
    is_flag=True,
    default=False,
    help="record current file digests into bundle.yaml (deliberate, reviewed action)",
)
def freeze_command(
    scenario_dir: Path, golden_dir: Path | None, out_dir: Path | None, write_manifest: bool
) -> None:
    """Record bundle digests, optionally promoting a run to the golden reference."""
    import yaml  # noqa: PLC0415 - only the deliberate freeze path touches disk

    from droneops_sim_runner.bundle import freeze_bundle  # noqa: PLC0415

    if write_manifest:
        manifest = yaml.safe_load((scenario_dir / "bundle.yaml").read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise click.ClickException(f"{scenario_dir / 'bundle.yaml'} is not a mapping")
        digest = freeze_bundle(scenario_dir, manifest)
        click.echo(f"bundle.yaml digests written; bundle_sha256={digest}")
        return

    digest = bundle_sha256(scenario_dir)
    click.echo(f"{scenario_dir.name} bundle_sha256={digest}")
    if golden_dir is not None and out_dir is not None:
        golden_dir.mkdir(parents=True, exist_ok=True)
        for name in REPLAY_ARTIFACTS:
            (golden_dir / name).write_bytes((out_dir / name).read_bytes())
        (golden_dir / "digest.txt").write_text(f"{artifact_digest(out_dir)}\n", encoding="utf-8")
        click.echo(f"golden updated at {golden_dir}")


@main.command("hash")
@click.option(
    "--scenario",
    "scenario_dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    required=True,
)
def hash_command(scenario_dir: Path) -> None:
    """Print the content addresses of a scenario bundle without running it."""
    load_bundle(scenario_dir)
    click.echo(f"bundle_sha256={bundle_sha256(scenario_dir)}")
    click.echo(f"scenario_sha256={scenario_digest(scenario_dir)}")
    click.echo(f"manifest_sha256={file_sha256(scenario_dir / MANIFEST_NAME)}")


@main.command("batch")
@click.option(
    "--scenario",
    "scenario_dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    required=True,
)
@click.option("--seeds", type=int, default=20, show_default=True)
@click.option("--base-seed", type=int, default=None, help="defaults to the bundle seed")
@click.option("--family", type=click.Choice(["routine", "rush", "failure"]), default=None)
@click.option("--out", "out_dir", type=click.Path(file_okay=False, path_type=Path), required=True)
def batch_command(
    scenario_dir: Path, seeds: int, base_seed: int | None, family: str | None, out_dir: Path
) -> None:
    """Run N seeded scenarios as a batch and print the deterministic metrics table."""
    if seeds < 20:
        raise click.ClickException("the Phase 3 preview gate requires at least 20 seeded scenarios")
    bundle = load_bundle(scenario_dir)
    digest = scenario_digest(scenario_dir)
    base = base_seed if base_seed is not None else bundle.seed
    rows: list[dict[str, int]] = []
    for index in range(seeds):
        seed = base + index * 1009  # fixed stride: no process-order or RNG leakage
        result = run_scenario(bundle, digest, seed=seed, family=family)
        write_run(out_dir / f"run-{seed}", result)
        rows.append(batch_summary_row(seed, result.metrics))
    (out_dir / "batch_summary.json").write_text(
        json.dumps({"scenario_id": bundle.scenario_id, "runs": rows}, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    header = (
        "seed",
        "sub",
        "acc",
        "comp",
        "late",
        "fail",
        "rej",
        "unsrv",
        "E_wh",
        "Wh/ord",
        "p95dur_s",
        "p95pad_s",
        "repoWh",
        "cost_p",
    )
    widths = (9, 4, 4, 5, 5, 5, 4, 6, 7, 7, 9, 9, 7, 7)
    click.echo("".join(name.rjust(width + 1) for name, width in zip(header, widths, strict=False)))
    for row in sorted(rows, key=lambda entry: entry["seed"]):
        cells = (
            row["seed"],
            row["submitted"],
            row["accepted"],
            row["completed"],
            row["late"],
            row["failed"],
            row["rejected"],
            row["unserved"],
            row["energy_wh"],
            row["wh_per_completion"],
            row["p95_duration_min_us"] // 1_000_000,
            row["p95_pad_wait_us"] // 1_000_000,
            row["reposition_wh"],
            row["cost_paise_per_delivery"],
        )
        click.echo(
            "".join(str(cell).rjust(width + 1) for cell, width in zip(cells, widths, strict=False))
        )
    medians = {
        key: sorted(int(row[key]) for row in rows)[len(rows) // 2]
        for key in ("completed", "late", "unserved", "energy_wh", "cost_paise_per_delivery")
    }
    click.echo(f"median over {seeds} seeds: {json.dumps(medians, sort_keys=True)}")
    click.echo("Simulation only. Not authorised for flight.")


if __name__ == "__main__":
    sys.exit(main())
