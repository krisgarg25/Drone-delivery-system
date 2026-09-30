"""Hashing: query identity, content identity and the canonical JSON that feeds both.

Cache keys and provenance hashes must be stable across runs and platforms, so:

* the query text is **normalised** (whitespace collapsed, no trailing newline) before
  hashing - a reformatted query is the same query;
* JSON is dumped with ``sort_keys=True`` and ASCII escapes - two dict insertion orders
  are the same document.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

HASH_PREFIX = "sha256:"
_CHUNK = 1024 * 1024


_PUNCTUATION = re.compile(r"\s*([;,\(\)\[\]\{\}])\s*")


def normalise_query(text: str) -> str:
    """Collapse a query to one canonical form so reformatting is not a new query.

    Quote-aware: tag values may legitimately contain spaces
    (``["amenity"="place of worship"]``), and stripping them would make two different
    queries hash identically. Outside quotes, whitespace runs collapse to one space and
    spacing around Overpass's structural punctuation is removed, so a query wrapped
    differently in a template still hits the same cache entry.
    """
    segments = text.split('"')
    if len(segments) % 2 == 0:
        # Unbalanced quoting: fall back to the conservative collapse rather than guess
        # where the literal ends.
        return " ".join(text.split())
    canonical: list[str] = []
    for index, segment in enumerate(segments):
        if index % 2 == 1:
            canonical.append(segment)  # inside a string literal: preserve verbatim
            continue
        collapsed = _PUNCTUATION.sub(r"\1", " ".join(segment.split()))
        # A space next to a string literal is a token separator, not formatting.
        if collapsed and index + 1 < len(segments) and segment[-1:].isspace() and segments[index + 1]:
            collapsed += " "
        if collapsed and index > 0 and segment[:1].isspace() and segments[index - 1]:
            collapsed = " " + collapsed
        canonical.append(collapsed)
    return '"'.join(canonical)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def query_sha256(text: str) -> str:
    """The cache key of a query: hex sha256 of its normalised UTF-8 bytes."""
    return sha256_hex(normalise_query(text).encode("utf-8"))


def canonical_json_bytes(value: Any) -> bytes:
    """Deterministic JSON encoding - the only canonical form this adapter uses."""
    return json.dumps(
        value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), default=str
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    """Prefixed sha256 of a JSON-serialisable value, e.g. ``sha256:ab12...``."""
    return HASH_PREFIX + sha256_hex(canonical_json_bytes(value))


def prefixed(hexdigest: str) -> str:
    return hexdigest if hexdigest.startswith(HASH_PREFIX) else HASH_PREFIX + hexdigest


def file_sha256(path: Path) -> str:
    """Prefixed sha256 of a file on disk, streamed so a 30 MB extract is cheap."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    return prefixed(digest.hexdigest())
