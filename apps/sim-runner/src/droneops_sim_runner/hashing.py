"""Content hashing shared by the scenario bundle and the configuration release.

Single implementation on purpose: `scripts/config_release.py` imports this module so the
digest a run records is the digest CI computes from the same tree.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

SKIP_NAMES = {".venv", "__pycache__", "node_modules", ".git", ".mypy_cache", ".ruff_cache"}
TEXT_SUFFIXES = {".yaml", ".yml", ".json", ".toml", ".md", ".txt", ".py"}


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def collect_file_digests(root: Path, inputs: tuple[str, ...], text_only: bool = True) -> list[dict[str, str]]:
    """Return {path, sha256} for every hashed file, sorted by POSIX path."""
    entries: list[dict[str, str]] = []
    for relative in sorted(inputs):
        base = root / relative
        if not base.exists():
            continue
        paths = [base] if base.is_file() else sorted(base.rglob("*"))
        for path in paths:
            if path.is_dir() or any(part in SKIP_NAMES for part in path.parts):
                continue
            if text_only and path.suffix.lower() not in TEXT_SUFFIXES:
                continue
            entries.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "sha256": file_sha256(path),
                }
            )
    return entries


def manifest_sha256(entries: list[dict[str, str]]) -> str:
    """Hash the canonical (path, digest) sequence; independent of filesystem ordering."""
    digest = hashlib.sha256()
    for entry in entries:
        digest.update(entry["path"].encode("utf-8"))
        digest.update(b"\0")
        digest.update(entry["sha256"].encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def bundle_sha256(bundle_dir: Path) -> str:
    """Hash every file in a scenario bundle directory, excluding the bundle manifest."""
    entries = [
        {"path": path.name, "sha256": file_sha256(path)}
        for path in sorted(bundle_dir.iterdir())
        if path.is_file() and path.name != "bundle.yaml"
    ]
    return manifest_sha256(entries)


def config_release_sha256(root: Path, inputs: tuple[str, ...] = ("configs", "contracts")) -> str:
    return manifest_sha256(collect_file_digests(root, inputs))
