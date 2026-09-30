"""Polite Overpass client: identity, rate limiting, backoff, caching, and a refusal
to invent.

Why each of these exists is documented in ``technical-build/12`` section 3 and was
re-measured on 2026-10-01 while building this module:

* ``overpass-api.de`` returned **HTTP 504** on a plain building count and again on a
  larger bbox, and recovered only on retry;
* a request without a project-identifying **User-Agent** is rejected (406 historically,
  and the endpoints' own error pages ask for it);
* ``overpass.openstreetmap.fr`` answers **403 "only available to white-listed usages"**,
  several hosts do not resolve, and one mirror that answered **200 with a valid-looking
  JSON body** reported ``timestamp_osm_base "117395"`` and zero elements for every
  Chandigarh query. A client without the plausibility guard below would have written
  "0 buildings in Chandigarh" into the evidence file, and that is a worse failure than
  a timeout because it looks like a measurement.
"""

from __future__ import annotations

import json
import random
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx

from .cache import CacheRecord, RawCache
from .config import OverpassSettings
from .errors import (
    EndpointUnavailableError,
    ImplausibleEndpointError,
    QueryError,
)
from .hashing import query_sha256
from .provenance import SOURCE_MODE_CACHE, SOURCE_MODE_LIVE, utc_now

ISO_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
RETRYABLE_STATUS = frozenset({408, 421, 429, 500, 502, 503, 504, 520, 522, 524})
FATAL_STATUS = frozenset({400, 406, 414})


@dataclass
class Attempt:
    """One HTTP attempt, kept so the report can show the truth of the run."""

    endpoint: str
    label: str
    status: int | None
    outcome: str
    elapsed_s: float
    slept_before_s: float = 0.0
    detail: str = ""
    query_sha256: str = ""
    at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "endpoint": self.endpoint,
            "label": self.label,
            "status": self.status,
            "outcome": self.outcome,
            "elapsed_s": round(self.elapsed_s, 3),
            "slept_before_s": round(self.slept_before_s, 3),
            "detail": self.detail,
            "query_sha256": self.query_sha256,
            "at": self.at,
        }


@dataclass
class FetchResult:
    """A response plus the story of how it was obtained.

    ``source_mode`` is ``live`` or ``cache``. ``cache`` is not a silent fallback: it
    sets ``degraded`` and carries a note that is copied into every downstream artefact.
    """

    query_text: str
    query_sha256: str
    record: CacheRecord
    payload: bytes
    source_mode: str
    degraded: bool = False
    note: str = ""
    attempts: list[Attempt] = field(default_factory=list)

    @property
    def retrieved_at(self) -> datetime:
        return datetime.fromisoformat(self.record.retrieved_at)

    def json(self) -> Any:
        return json.loads(self.payload.decode("utf-8"))

    def attempt_log(self) -> list[dict[str, Any]]:
        return [attempt.as_dict() for attempt in self.attempts]


class RateLimiter:
    """Minimum spacing between outbound requests. Never a token bucket that forgives."""

    def __init__(self, minimum_interval_s: float, *, clock: Callable[[], float] = time.monotonic):
        self.minimum_interval_s = minimum_interval_s
        self.clock = clock
        self._last_request: float | None = None

    def wait(self, sleeper: Callable[[float], None]) -> float:
        """Sleep until the next legal request time; return how long we slept."""
        now = self.clock()
        if self._last_request is None:
            self._last_request = now
            return 0.0
        due = self._last_request + self.minimum_interval_s
        delay = max(0.0, due - now)
        if delay > 0:
            sleeper(delay)
        self._last_request = self.clock()
        return delay

    def force_elapsed(self, seconds: float) -> None:
        """Test hook: pretend ``seconds`` of wall time have passed."""
        self._last_request = self.clock() - seconds


class OverpassClient:
    """POST a query, get an answer we can trust, or raise with the full attempt log."""

    def __init__(
        self,
        settings: OverpassSettings,
        cache: RawCache,
        *,
        live: bool | None = None,
        transport: httpx.BaseTransport | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self.settings = settings
        # Validated here as well as at config load: the checks below are what stops a
        # hand-built client from hammering a volunteer endpoint anonymously.
        settings.validate()
        self.cache = cache
        self.live = settings.allow_live if live is None else live
        self.sleeper = sleeper
        self.now = now
        self.limiter = RateLimiter(settings.request_minimum_interval_s, clock=clock)
        self._cooldowns: dict[str, float] = {}
        self._suspicious: dict[str, int] = {}
        self._rng = random.Random(settings.retry_seed)
        self.attempts: list[Attempt] = []
        self._client = httpx.Client(
            transport=transport,
            timeout=httpx.Timeout(settings.request_timeout_s),
            follow_redirects=True,
            headers={
                "User-Agent": settings.user_agent,
                "Accept": "application/json;q=0.9,*/*;q=0.5",
                "Accept-Encoding": "gzip, deflate",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )

    # --------------------------------------------------------------- public API
    def fetch(
        self,
        query_text: str,
        *,
        label: str = "",
        min_expected_total: int = 0,
        component: str = "",
    ) -> FetchResult:
        """Retrieve a query live when possible, otherwise degrade to cache loudly.

        ``min_expected_total`` is the plausibility floor: a 200 response whose element
        total is below it is treated as a broken mirror, not as a measurement. Pass 0
        where zero is a legitimate answer (a tight box with nothing in it).
        """
        key = query_sha256(query_text)
        if self.live:
            try:
                record = self._request_live(
                    query_text, label=label, key=key, min_expected_total=min_expected_total
                )
            except (EndpointUnavailableError, ImplausibleEndpointError):
                cached = self.cache.latest(query_text, label=label)
                if cached is None:
                    raise
                return self._from_cache(query_text, cached, reason="endpoint unavailable")
            return FetchResult(
                query_text=query_text,
                query_sha256=key,
                record=record,
                payload=record.body(),
                source_mode=SOURCE_MODE_LIVE,
                attempts=list(self.attempts),
                note=f"live retrieval ({component})" if component else "live retrieval",
            )
        cached = self.cache.latest(query_text, label=label)
        if cached is None:
            raise EndpointUnavailableError(
                f"live retrieval disabled and no cached response exists for query "
                f"{key} (label {label or '-'}). Nothing was fabricated to fill the gap.",
                [attempt.as_dict() for attempt in self.attempts],
            )
        return self._from_cache(query_text, cached, reason="live retrieval disabled", label=label)

    def _from_cache(
        self, query_text: str, record: CacheRecord, *, reason: str, label: str = ""
    ) -> FetchResult:
        note = (
            f"DEGRADED: served from raw cache ({reason}). Key sha256={record.query_sha256}, "
            f"retrieved_at={record.retrieved_at}, endpoint={record.endpoint or 'unknown'}. "
            f"Any 'fresh' claim for this component is void until it is re-retrieved; "
            f"the data itself is unchanged and still carries its own hash."
        )
        self.attempts.append(
            Attempt(
                endpoint=record.endpoint or "cache",
                label=label,
                status=record.http_status or None,
                outcome="cache_fallback",
                elapsed_s=0.0,
                detail=reason,
                query_sha256=record.query_sha256,
                at=self.now().isoformat(),
            )
        )
        return FetchResult(
            query_text=query_text,
            query_sha256=record.query_sha256,
            record=record,
            payload=record.body(),
            source_mode=SOURCE_MODE_CACHE,
            degraded=True,
            note=note,
            attempts=list(self.attempts),
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> OverpassClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -------------------------------------------------------------- internals
    def _request_live(
        self, query_text: str, *, label: str, key: str, min_expected_total: int = 0
    ) -> CacheRecord:
        endpoints = self._endpoints_available()
        if not endpoints:
            raise EndpointUnavailableError(
                "every configured Overpass endpoint is in cooldown after repeated "
                "failures; refusing to invent data",
                [attempt.as_dict() for attempt in self.attempts],
            )
        failures: list[str] = []
        for endpoint in endpoints:
            for attempt_number in range(1, self.settings.max_attempts_per_endpoint + 1):
                slept = self.limiter.wait(self.sleeper)
                started = time.monotonic()
                try:
                    response = self._client.post(endpoint, data={"data": query_text})
                except httpx.HTTPError as exc:
                    elapsed = time.monotonic() - started
                    detail = f"{type(exc).__name__}: {exc}"
                    failures.append(f"{endpoint} attempt {attempt_number}: {detail}")
                    self.attempts.append(
                        Attempt(
                            endpoint=endpoint,
                            label=label,
                            status=None,
                            outcome="transport_error",
                            elapsed_s=elapsed,
                            slept_before_s=slept,
                            detail=detail[:400],
                            query_sha256=key,
                            at=self.now().isoformat(),
                        )
                    )
                    self._sleep_before_retry(endpoint, attempt_number, key)
                    continue

                elapsed = time.monotonic() - started
                if response.status_code in RETRYABLE_STATUS:
                    detail = _first_line(response.text)
                    failures.append(f"{endpoint} HTTP {response.status_code}: {detail}")
                    self.attempts.append(
                        Attempt(
                            endpoint=endpoint,
                            label=label,
                            status=response.status_code,
                            outcome="retryable_status",
                            elapsed_s=elapsed,
                            slept_before_s=slept,
                            detail=detail[:400],
                            query_sha256=key,
                            at=self.now().isoformat(),
                        )
                    )
                    self._sleep_before_retry(
                        endpoint, attempt_number, key, retry_after=response.headers.get("Retry-After")
                    )
                    continue

                if response.status_code in FATAL_STATUS:
                    detail = _first_line(response.text)
                    self.attempts.append(
                        Attempt(
                            endpoint=endpoint,
                            label=label,
                            status=response.status_code,
                            outcome="rejected",
                            elapsed_s=elapsed,
                            slept_before_s=slept,
                            detail=detail[:400],
                            query_sha256=key,
                            at=self.now().isoformat(),
                        )
                    )
                    if response.status_code == 406:
                        raise QueryError(
                            f"{endpoint} rejected the request with 406. Historically this "
                            "means the User-Agent is missing or generic; current UA is "
                            f"{self.settings.user_agent!r}. Query was not retried because "
                            "retrying an identical rejected request is the behaviour the "
                            "endpoint operators complain about."
                        )
                    raise QueryError(f"{endpoint} rejected the query ({detail[:200]})")

                try:
                    payload = self._decode(response)
                    replication = self._validate_body(
                        payload, endpoint=endpoint, label=label, min_expected_total=min_expected_total
                    )
                except ImplausibleEndpointError as exc:
                    self.attempts.append(
                        Attempt(
                            endpoint=endpoint,
                            label=label,
                            status=response.status_code,
                            outcome="implausible_body",
                            elapsed_s=elapsed,
                            slept_before_s=slept,
                            detail=str(exc)[:400],
                            query_sha256=key,
                            at=self.now().isoformat(),
                        )
                    )
                    self._suspicious[endpoint] = self._suspicious.get(endpoint, 0) + 1
                    if self._suspicious[endpoint] >= self.settings.max_consecutive_suspicious:
                        self._cooldowns[endpoint] = time.monotonic() + self.settings.endpoint_cooldown_s
                    # Kept as evidence of the mirror, excluded from replay: the body is
                    # written under _quarantine so a refused zero can never come back as
                    # a cached measurement on the next offline run.
                    self.cache.quarantine(
                        query_text,
                        response.content,
                        reason=str(exc),
                        endpoint=endpoint,
                        retrieved_at=self.now(),
                        http_status=response.status_code,
                        label=label,
                    )
                    failures.append(f"{endpoint} implausible: {exc}")
                    break

                self._suspicious[endpoint] = 0
                self.attempts.append(
                    Attempt(
                        endpoint=endpoint,
                        label=label,
                        status=response.status_code,
                        outcome="ok",
                        elapsed_s=elapsed,
                        slept_before_s=slept,
                        detail="",
                        query_sha256=key,
                        at=self.now().isoformat(),
                    )
                )
                retrieved = self.now()
                try:
                    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
                        "utf-8"
                    )
                except (TypeError, ValueError):  # pragma: no cover - already parsed
                    body = response.content
                return self.cache.store(
                    query_text,
                    body,
                    retrieved_at=retrieved,
                    endpoint=endpoint,
                    source_mode=SOURCE_MODE_LIVE,
                    http_status=response.status_code,
                    replication_timestamp=replication,
                    label=label,
                    note=f"{len(payload.get('elements', []))} elements",
                )

            self._cooldowns[endpoint] = time.monotonic() + self.settings.endpoint_cooldown_s

        raise EndpointUnavailableError(
            "no configured Overpass endpoint returned a trustworthy response for this "
            "query, and no cached copy exists. This ETL does not fabricate geometries: "
            "the layer is absent, and the manifest will say so.\n"
            + "\n".join(f"  - {line}" for line in failures[:12]),
            [attempt.as_dict() for attempt in self.attempts],
        )

    def _decode(self, response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise ImplausibleEndpointError(
                f"{response.url} returned HTTP {response.status_code} with a body that "
                f"is not JSON: {_first_line(response.text)[:160]}"
            ) from exc
        if not isinstance(payload, dict) or "elements" not in payload:
            raise ImplausibleEndpointError(
                f"{response.url} returned a JSON body with no 'elements' array; keys="
                f"{sorted(payload)[:8] if isinstance(payload, dict) else type(payload).__name__}"
            )
        return payload

    def _validate_body(
        self, payload: dict[str, Any], *, endpoint: str, label: str, min_expected_total: int = 0
    ) -> str:
        """Reject a mirror that answers 200 with an impossible database state."""
        osm3s = payload.get("osm3s") or {}
        replication = str(osm3s.get("timestamp_osm_base", ""))
        if self.settings.require_iso_replication_timestamp and not ISO_TIMESTAMP.match(replication):
            raise ImplausibleEndpointError(
                f"{endpoint} reports timestamp_osm_base={replication!r}, which is not "
                "an ISO-8601 UTC timestamp. This mirror's database state cannot be "
                f"recorded as provenance ({label or 'unlabelled'}); treating its "
                "answers as measurements would corrupt the dataset."
            )
        if min_expected_total > 0:
            total = result_total(payload)
            if total < min_expected_total:
                raise ImplausibleEndpointError(
                    f"{endpoint} answered HTTP 200 with a result total of {total} where at "
                    f"least {min_expected_total} was expected ({label or 'unlabelled'}). "
                    "A mirror returning an empty or stale database looks identical to a "
                    "genuine zero in the output, so it is rejected here rather than "
                    "recorded as a measurement."
                )
        return replication

    def _endpoints_available(self) -> Sequence[str]:
        now = time.monotonic()
        return [
            endpoint
            for endpoint in self.settings.endpoints
            if self._cooldowns.get(endpoint, 0.0) <= now
        ]

    def _sleep_before_retry(
        self, endpoint: str, attempt_number: int, key: str, *, retry_after: str | None = None
    ) -> None:
        if attempt_number >= self.settings.max_attempts_per_endpoint:
            return
        backoff = min(
            self.settings.backoff_cap_s,
            self.settings.backoff_base_s * (self.settings.backoff_multiplier ** (attempt_number - 1)),
        )
        # Jitter is seeded per (retry_seed, query, endpoint, attempt) so a re-run of a
        # failed sequence is reproducible while the fleet of queries does not synchronise
        # its retries into a second wave of load.
        rng = random.Random(f"{self.settings.retry_seed}:{key}:{endpoint}:{attempt_number}")
        delay = backoff + rng.uniform(0.0, self.settings.jitter_max_s)
        delay = max(delay, _retry_after_seconds(retry_after)) if retry_after else delay
        self.sleeper(delay)


def _retry_after_seconds(raw: str) -> float:
    """A server's own pacing request, in seconds; unparseable values ask for nothing."""
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def _first_line(text: str) -> str:
    stripped = text.strip()
    return stripped.splitlines()[0] if stripped else ""


def result_total(payload: dict[str, Any]) -> int:
    """How much an answer actually contains.

    ``out count`` yields one synthetic ``type: count`` element carrying the totals;
    ``len(elements)`` for that response is always 1, which would make every count
    query look plausible to a naive emptiness check. This function reads the totals.
    """
    elements = payload.get("elements") or []
    counts = [element for element in elements if element.get("type") == "count"]
    if counts:
        return sum(_as_int((element.get("tags") or {}).get("total")) for element in counts)
    return len(elements)


def count_breakdown(payload: dict[str, Any]) -> dict[str, int]:
    """The nodes/ways/relations/total breakdown of a single ``out count`` response."""
    nodes = ways = relations = total = 0
    for element in payload.get("elements") or []:
        if element.get("type") != "count":
            continue
        tags = element.get("tags") or {}
        nodes += _as_int(tags.get("nodes"))
        ways += _as_int(tags.get("ways"))
        relations += _as_int(tags.get("relations"))
        total += _as_int(tags.get("total"))
    return {"nodes": nodes, "ways": ways, "relations": relations, "total": total}


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


__all__ = ["Attempt", "FetchResult", "OverpassClient", "RateLimiter"]
