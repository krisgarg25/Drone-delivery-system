"""Configuration-release manifest hash.

A run, a plan and a decision must all record `config_release_sha256`. The hash covers
every safety and policy input: separation parameters, altitude-reference rules, contract
schemas and scenario policy files. It is content-addressed and ordering independent, so
an identical configuration always produces an identical hash on any machine.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INPUTS: tuple[str, ...] = ("configs", "contracts")
SKIP_NAMES = {".venv", "__pycache__", "node_modules", ".git"}
TEXT_SUFFIXES = {".yaml", ".yml", ".json", ".toml", ".md", ".txt", ".py"}


def digest_for(root: Path, inputs: tuple[str, ...]) -> tuple[str, list[dict[str, str]]]:
    entries: list[dict[str, str]] = []
    for relative in inputs:
        base = root / relative
        if not base.exists():
            continue
        for path in sorted(base.rglob("*")):
            if any(part in SKIP_NAMES for part in path.parts):
                continue
            if not path.is_file():
                continue
            if path.suffix.lower() not in TEXT_SUFFIXES:
                continue
            payload = path.read_bytes()
            entries.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
            )

    # Sorted by path already; hash the canonical (path, digest) pairs only.
    manifest = hashlib.sha256()
    for entry in entries:
        manifest.update(entry["path"].encode("utf-8"))
        manifest.update(b"\0")
        manifest.update(entry["sha256"].encode("utf-8"))
        manifest.update(b"\0")
    return manifest.hexdigest(), entries


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", nargs="+", default=list(DEFAULT_INPUTS))
    parser.add_argument("--json", action="store_true", help="emit machine-readable output")
    parser.add_argument("--out", type=Path, help="write the manifest to a file")
    args = parser.parse_args(argv)

    digest, entries = digest_for(REPO_ROOT, tuple(args.inputs))
    payload: dict[str, Any] = {"config_release_sha256": digest, "file_count": len(entries), "files": entries}

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"{digest} ({len(entries)} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:] if len(sys.argv) > 1 else []))
