"""Licence allowlist gate.

Only MIT, BSD, Apache-2.0 and ISC are permitted for third-party code
(`07-infrastructure-ci-security.md` section 12). Anything else fails the build, so a
network-copyleft or source-available dependency cannot be merged by accident.

Two deliberate escape hatches, both explicit and both owned by the dependency owner via
CODEOWNERS:

* `droneops-*` first-party distributions are internal proprietary code (LICENSE.md) and
  carry no SPDX licence; they are skipped by name prefix.
* `--exceptions` lists `package licence-token` pairs that a human reviewed, e.g. numpy's
  bundled CC0-1.0 sub-libraries. An exception never covers a family, only one package.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

FIRST_PARTY_PREFIXES = ("droneops-", "droneops_workspace")
# Descriptive badges that some metadata files list next to the real licence.
NON_LICENCE_LABELS = {"dfsg approved", "osi approved", "license"}


def normalise(licence: str) -> str:
    """Collapse the many spellings of a licence into one comparable family token."""
    compact = re.sub(r"[^a-z0-9.+-]+", " ", licence.lower()).strip()
    if compact.startswith("mit"):
        return "mit"
    if compact.startswith("apache") or "apache-2.0" in compact:
        return "apache-2.0"
    if compact.startswith("isc") or "isc license" in compact:
        return "isc"
    if "bsd" in compact:
        return "bsd"
    if "mozilla" in compact or "mpl" in compact:
        return "mpl-2.0"
    if "python software foundation" in compact or compact.startswith("psf"):
        return "psf"
    if "apache" in compact:
        return "apache-2.0"
    if compact.startswith("cc0"):
        return "cc0-1.0"
    if compact.startswith("zlib"):
        return "zlib"
    return compact.replace(" ", "-") or "unknown"


def split_expression(licence: str) -> list[str]:
    parts = re.split(r"\s+(?:AND|OR|and|or)\s+|[;]", licence)
    return [part.strip() for part in parts if part.strip()]


def load_tokens(path: Path) -> set[str]:
    tokens: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.split("#", 1)[0].strip()
        if stripped:
            tokens.add(stripped.lower())
    return tokens


def pip_licenses(args: argparse.Namespace) -> list[dict[str, Any]]:
    command = [sys.executable, "-m", "piplicenses", "--format", "json"]
    if args.requirements:
        command += ["--requirements", str(args.requirements)]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        print(result.stderr or result.stdout, file=sys.stderr)
        raise SystemExit(result.returncode)
    packages: list[dict[str, Any]] = json.loads(result.stdout)
    return packages


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allowlist", type=Path, required=True)
    parser.add_argument("--exceptions", type=Path)
    parser.add_argument("--requirements", type=Path, help="audit only these pinned requirements")
    parser.add_argument("--report", type=Path, help="optional JSON report path")
    args = parser.parse_args(argv)

    if not args.allowlist.exists():
        print(f"allowlist {args.allowlist} is missing", file=sys.stderr)
        return 2

    allowed = {normalise(token) for token in load_tokens(args.allowlist)}
    exceptions: set[str] = set()
    if args.exceptions:
        exceptions = load_tokens(args.exceptions)

    offenders: list[dict[str, str]] = []
    first_party = 0
    packages = pip_licenses(args)
    for package in packages:
        name = str(package.get("Name", "?"))
        version = str(package.get("Version", "?"))
        if name.lower().startswith(FIRST_PARTY_PREFIXES):
            first_party += 1
            continue
        for part in split_expression(str(package.get("License", "") or "")):
            key = normalise(part)
            if key in NON_LICENCE_LABELS:
                continue
            if key in allowed:
                continue
            if f"{name.lower()} {key}" in exceptions:
                continue
            offenders.append({"name": name, "version": version, "licence": part})

    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps({"packages": packages}, indent=2), encoding="utf-8")

    print(
        f"{len(packages) - first_party} third-party distributions checked "
        f"(+{first_party} first-party skipped) against {sorted(allowed)}"
    )
    if offenders:
        print("LICENCE GATE FAILED:", file=sys.stderr)
        for offender in sorted(offenders, key=lambda o: (o["name"].lower(), o["licence"])):
            print(
                f"  - {offender['name']} {offender['version']}: {offender['licence']} "
                f"(add '{offender['name'].lower()} {normalise(offender['licence'])} # reason' "
                "to the exception file only after review)",
                file=sys.stderr,
            )
        return 1
    print("all licences allowed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
