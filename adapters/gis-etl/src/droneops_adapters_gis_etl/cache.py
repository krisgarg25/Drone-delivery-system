"""Content-addressed raw cache.

Layout::

    data/raw/overpass/<query_sha256>/<retrieved_at>+<response_sha256[0:8]>.json
    data/raw/overpass/<query_sha256>/<...>.meta.json
    data/raw/index.jsonl

The key is the sha256 of the *normalised* query; the retrieval timestamp is part of
the filename, so the same query retrieved twice keeps both answers and the newest one
is what a replay picks up. Every response body is cached, including the ones the run
ended up not using: a provenance trail that discards evidence on the floor is not a
provenance trail.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .errors import CacheError, CacheMissError
from .hashing import file_sha256, prefixed, query_sha256, sha256_hex

INDEX_NAME = "index.jsonl"
#: Bodies refused by the plausibility guard. Prefixed with an underscore so no query
#: hash can collide with it, and one directory deeper than the replay path looks at.
QUARANTINE_DIRNAME = "_quarantine"


def _stamp(retrieved_at: datetime) -> str:
    return retrieved_at.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


@dataclass(frozen=True)
class CacheRecord:
    """One cached response and how it was obtained."""

    key: str
    query_sha256: str
    retrieved_at: str
    path: str
    meta_path: str
    response_sha256: str
    endpoint: str = ""
    query_text: str = ""
    source_mode: str = "live"
    http_status: int = 0
    replication_timestamp: str = ""
    note: str = ""
    label: str = ""
    extra: dict[str, object] = field(default_factory=dict)

    def body(self) -> bytes:
        path = Path(self.path)
        if not path.is_file():
            raise CacheMissError(f"cached body missing for {self.key}: {path}")
        return path.read_bytes()

    def json(self) -> object:
        return json.loads(self.body().decode("utf-8"))


class RawCache:
    """Filesystem cache of every byte retrieved from outside the repo."""

    def __init__(self, raw_dir: Path, namespace: str = "overpass") -> None:
        self.root = Path(raw_dir)
        self.namespace = namespace
        self.dir = self.root / namespace
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.root / INDEX_NAME).parent.mkdir(parents=True, exist_ok=True)

    @property
    def index_path(self) -> Path:
        return self.root / INDEX_NAME

    def key_for(self, query_text: str) -> str:
        return query_sha256(query_text)

    def latest(self, query_text: str, *, label: str = "") -> CacheRecord | None:
        """The newest cached response for this query, or None.

        Matching is on the query hash *and* the label when one is given, so a
        chunk-specific cache entry is never mistaken for the whole layer.
        """
        records = list(self.records(query_text, label=label))
        if not records:
            return None
        return max(records, key=lambda record: record.retrieved_at)

    def records(self, query_text: str, *, label: str = "") -> Iterator[CacheRecord]:
        directory = self.dir / query_sha256(query_text)
        if not directory.is_dir():
            return iter(())
        found: list[CacheRecord] = []
        for meta_path in sorted(directory.glob("*.meta.json")):
            try:
                payload = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:  # pragma: no cover - corrupt
                raise CacheError(f"unreadable cache sidecar {meta_path}: {exc}") from exc
            record = CacheRecord(**payload)
            if label and record.label != label:
                continue
            found.append(record)
        return iter(found)

    def store(
        self,
        query_text: str,
        payload: bytes,
        *,
        retrieved_at: datetime,
        endpoint: str,
        source_mode: str = "live",
        http_status: int = 0,
        replication_timestamp: str = "",
        note: str = "",
        label: str = "",
        extra: dict[str, object] | None = None,
    ) -> CacheRecord:
        """Write a response body plus its sidecar, then append to the index."""
        key = query_sha256(query_text)
        directory = self.dir / key
        directory.mkdir(parents=True, exist_ok=True)
        digest = sha256_hex(payload)
        body_path = directory / f"{_stamp(retrieved_at)}+{digest[:8]}.json"
        meta_path = directory / f"{_stamp(retrieved_at)}+{digest[:8]}.meta.json"
        record = CacheRecord(
            key=key,
            query_sha256=key,
            retrieved_at=retrieved_at.astimezone(UTC).isoformat(),
            path=str(body_path),
            meta_path=str(meta_path),
            response_sha256=prefixed(digest),
            endpoint=endpoint,
            query_text=" ".join(query_text.split()),
            source_mode=source_mode,
            http_status=http_status,
            replication_timestamp=replication_timestamp,
            note=note,
            label=label,
            extra=extra or {},
        )
        # Body first, then sidecar: a crash between the two leaves an orphan body,
        # which is recoverable; a sidecar without a body would advertise a cache hit
        # that cannot be read.
        body_path.write_bytes(payload)
        meta_path.write_text(json.dumps(asdict(record), indent=2, sort_keys=True) + "\n", "utf-8")
        self._append_index(record)
        return record

    def quarantine(
        self,
        query_text: str,
        payload: bytes,
        *,
        reason: str,
        endpoint: str,
        retrieved_at: datetime,
        http_status: int = 0,
        label: str = "",
    ) -> Path:
        """Keep a rejected body as evidence without ever letting it be replayed.

        An untrustworthy mirror's answer is still a fact about that mirror, so it is not
        deleted - but it must never satisfy ``latest()``, or a quarantined zero becomes a
        cached measurement on the next offline run. Quarantined entries therefore live
        under ``_quarantine/<key>/``, outside the directory the replay reads.
        """
        key = query_sha256(query_text)
        directory = self.dir / QUARANTINE_DIRNAME / key
        directory.mkdir(parents=True, exist_ok=True)
        digest = sha256_hex(payload)
        body_path = directory / f"{_stamp(retrieved_at)}+{digest[:8]}.json"
        body_path.write_bytes(payload)
        sidecar = {
            "quarantined": True,
            "reason": reason,
            "endpoint": endpoint,
            "key": key,
            "query_sha256": key,
            "query_text": " ".join(query_text.split()),
            "retrieved_at": retrieved_at.astimezone(UTC).isoformat(),
            "http_status": http_status,
            "label": label,
            "response_sha256": prefixed(digest),
            "path": str(body_path),
        }
        (directory / f"{_stamp(retrieved_at)}+{digest[:8]}.meta.json").write_text(
            json.dumps(sidecar, indent=2, sort_keys=True) + "\n", "utf-8"
        )
        self._append_index(
            CacheRecord(
                key=key,
                query_sha256=key,
                retrieved_at=retrieved_at.astimezone(UTC).isoformat(),
                path=str(body_path),
                meta_path=str(directory / f"{_stamp(retrieved_at)}+{digest[:8]}.meta.json"),
                response_sha256=prefixed(digest),
                endpoint=endpoint,
                query_text=" ".join(query_text.split()),
                source_mode="quarantined",
                http_status=http_status,
                label=label,
                note=reason,
            )
        )
        return body_path

    def quarantined(self) -> list[dict[str, object]]:
        """Every body this project refused to believe."""
        found: list[dict[str, object]] = []
        for meta_path in sorted((self.dir / QUARANTINE_DIRNAME).glob("*/*.meta.json")):
            try:
                found.append(json.loads(meta_path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):  # pragma: no cover - corrupt sidecar
                continue
        return found

    def _append_index(self, record: CacheRecord) -> None:
        line = json.dumps(
            {
                "at": record.retrieved_at,
                "namespace": self.namespace,
                "key": record.key,
                "label": record.label,
                "endpoint": record.endpoint,
                "source_mode": record.source_mode,
                "http_status": record.http_status,
                "response_sha256": record.response_sha256,
                "path": Path(record.path).relative_to(self.root).as_posix(),
                "replication_timestamp": record.replication_timestamp,
            },
            sort_keys=True,
        )
        try:
            with self.index_path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError as exc:  # pragma: no cover - filesystem failure
            raise CacheError(f"cannot append to {self.index_path}: {exc}") from exc

    def verify(self, record: CacheRecord) -> str:
        """Re-hash a cached body: provenance claims must be checkable, not trusted."""
        return file_sha256(Path(record.path))

    def all_entries(self) -> list[CacheRecord]:
        records: list[CacheRecord] = []
        for meta_path in sorted(self.dir.glob("*/*.meta.json")):
            try:
                records.append(CacheRecord(**json.loads(meta_path.read_text(encoding="utf-8"))))
            except (OSError, json.JSONDecodeError, TypeError):  # pragma: no cover
                continue
        return records
