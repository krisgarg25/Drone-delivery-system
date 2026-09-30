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
from droneops_sim_runner.kernel import run_scenario, write_run

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
@click.option("--scenario", "scenario_dir", type=click.Path(exists=True, file_okay=False, path_type=Path), required=True)
@click.option("--out", "out_dir", type=click.Path(file_okay=False, path_type=Path), required=True)
def run_command(scenario_dir: Path, out_dir: Path) -> None:
    """Execute a frozen bundle and write events, metrics and a report."""
    bundle = load_bundle(scenario_dir)
    result = run_scenario(bundle, scenario_digest(scenario_dir))
    write_run(out_dir, result)
    click.echo(f"run complete: {result.identity.scenario_id} -> {out_dir}")
    click.echo(f"bundle_sha256={result.identity.bundle_sha256}")
    click.echo(f"config_release_sha256={result.identity.config_release_sha256}")
    click.echo(json.dumps(result.metrics, indent=2, sort_keys=True))
    click.echo("Simulation only. Not authorised for flight.")


@main.command("replay")
@click.option("--scenario", "scenario_dir", type=click.Path(exists=True, file_okay=False, path_type=Path), required=True)
@click.option("--golden", "golden_dir", type=click.Path(exists=True, file_okay=False, path_type=Path), required=True)
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
@click.option("--scenario", "scenario_dir", type=click.Path(exists=True, file_okay=False, path_type=Path), required=True)
@click.option("--golden", "golden_dir", type=click.Path(file_okay=False, path_type=Path), default=None)
@click.option("--out", "out_dir", type=click.Path(file_okay=False, path_type=Path), default=None)
@click.option(
    "--write-manifest",
    is_flag=True,
    default=False,
    help="record current file digests into bundle.yaml (deliberate, reviewed action)",
)
def freeze_command(scenario_dir: Path, golden_dir: Path | None, out_dir: Path | None, write_manifest: bool) -> None:
    """Record bundle digests, optionally promoting a run to the golden reference."""
    import yaml

    from droneops_sim_runner.bundle import freeze_bundle

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
@click.option("--scenario", "scenario_dir", type=click.Path(exists=True, file_okay=False, path_type=Path), required=True)
def hash_command(scenario_dir: Path) -> None:
    """Print the content addresses of a scenario bundle without running it."""
    load_bundle(scenario_dir)
    click.echo(f"bundle_sha256={bundle_sha256(scenario_dir)}")
    click.echo(f"scenario_sha256={scenario_digest(scenario_dir)}")
    click.echo(f"manifest_sha256={file_sha256(scenario_dir / MANIFEST_NAME)}")


if __name__ == "__main__":
    sys.exit(main())
