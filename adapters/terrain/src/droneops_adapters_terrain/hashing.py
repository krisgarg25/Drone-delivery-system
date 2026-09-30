"""File hashing for the terrain adapter.

Deliberately tiny and local: the terrain adapter must be installable without the gis-etl
adapter, and a cross-adapter import would couple two things that are supposed to fail
independently (an ETL outage should not stop a DEM clip, and vice versa).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

_CHUNK = 1024 * 1024
_PREFIX = "sha256:"


def sha256_of_bytes(payload: bytes) -> str:
    return _PREFIX + hashlib.sha256(payload).hexdigest()


def sha256_of_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    return _PREFIX + digest.hexdigest()
