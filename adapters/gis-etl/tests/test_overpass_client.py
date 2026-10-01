"""Politeness and failure behaviour of the Overpass client.

These are the properties the Chandigarh measurement session forced into the design:
406 without a user agent, 504 under load, one mirror that returns a well-formed body
full of zeros, and a strict rule that none of it may be papered over with invented data.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from gis_etl_support import (
    GOOD_TIMESTAMP,
    RETRIEVED_AT,
    MockTransport,
    SleepRecorder,
    count_payload,
    make_client,
    overpass_payload,
)
from droneops_adapters_gis_etl.cache import RawCache
from droneops_adapters_gis_etl.config import GisConfig
from droneops_adapters_gis_etl.errors import (
    EndpointUnavailableError,
    ImplausibleEndpointError,
    QueryError,
)
from droneops_adapters_gis_etl.hashing import query_sha256
from droneops_adapters_gis_etl.overpass import OverpassClient, RateLimiter

QUERY = '[out:json][timeout:25];nwr[building](30.68,76.72,30.78,76.86);out count;'


def test_request_carries_the_project_user_agent(config: GisConfig, cache: RawCache) -> None:
    client, transport = make_client(
        config, cache, [(200, count_payload(1200))], sleeper=SleepRecorder()
    )
    client.fetch(QUERY, label="ua")
    assert transport.requests, "no request was made"
    assert transport.requests[0]["user_agent"] == config.overpass.user_agent
    assert "DroneOps" in transport.requests[0]["user_agent"]
    assert "contact" in transport.requests[0]["user_agent"].lower()
    client.close()


def test_user_agent_without_identity_is_a_config_error(config: GisConfig, cache: RawCache) -> None:
    from dataclasses import replace

    settings = replace(config.overpass, user_agent="python-httpx/0.28")
    with pytest.raises(Exception, match="user_agent"):
        OverpassClient(settings, cache).close()


def test_406_is_not_retried_and_says_why(config: GisConfig, cache: RawCache) -> None:
    sleeper = SleepRecorder()
    client, transport = make_client(
        config, cache, [(406, "Forbidden: missing user agent"), (200, count_payload(1))], sleeper=sleeper
    )
    with pytest.raises(QueryError, match="406"):
        client.fetch(QUERY, label="forbidden")
    # One attempt only: retrying an identical rejected request is endpoint abuse.
    assert len(transport.requests) == 1
    client.close()


def test_504_is_retried_with_growing_backoff(config: GisConfig, cache: RawCache) -> None:
    sleeper = SleepRecorder()
    client, transport = make_client(
        config,
        cache,
        [
            (504, "<html>Gateway Timeout</html>"),
            (504, "<html>Gateway Timeout</html>"),
            (200, count_payload(33_351)),
        ],
        sleeper=sleeper,
    )
    result = client.fetch(QUERY, label="flaky")
    assert result.record.http_status == 200
    assert len(transport.requests) == 3
    assert len(sleeper.slept) == 2
    assert sleeper.slept[1] > sleeper.slept[0], sleeper.slept
    # base 1.0 doubling, cap 10, jitter up to 0.5
    assert 1.0 <= sleeper.slept[0] <= 1.5
    assert 2.0 <= sleeper.slept[1] <= 2.5
    client.close()


def test_backoff_jitter_is_seeded_and_reproducible(config: GisConfig, cache: RawCache) -> None:
    def run_once() -> list[float]:
        sleeper = SleepRecorder()
        client, _ = make_client(
            config, cache, [(504, "busy"), (504, "busy"), (200, count_payload(5))], sleeper=sleeper
        )
        client.fetch(QUERY, label="seeded")
        client.close()
        return [round(value, 9) for value in sleeper.slept]

    assert run_once() == run_once()
    assert len(run_once()) == 2


def test_retry_after_is_honoured(config: GisConfig, cache: RawCache) -> None:
    sleeper = SleepRecorder()
    answers = [
        lambda: httpx.Response(429, text="rate limited", headers={"Retry-After": "30"}),
        (200, count_payload(9)),
    ]
    transport = MockTransport([])

    def handler(request: httpx.Request) -> httpx.Response:
        transport.requests.append({"url": str(request.url), "user_agent": None, "body": ""})
        answer = answers.pop(0)
        return answer() if callable(answer) else httpx.Response(answer[0], text=json.dumps(answer[1]))

    client = OverpassClient(
        config.overpass,
        cache,
        transport=httpx.MockTransport(handler),
        sleeper=sleeper,
    )
    client.fetch(QUERY, label="ratelimited")
    assert sleeper.slept[0] >= 30.0
    client.close()


def test_min_interval_between_requests(config: GisConfig, cache: RawCache) -> None:
    clock_values = iter([0.0, 0.2, 1.0, 2.0])
    limiter = RateLimiter(1.0, clock=lambda: next(clock_values))
    slept: list[float] = []
    assert limiter.wait(slept.append) == 0.0  # first request is free
    assert slept == []
    assert limiter.wait(slept.append) == pytest.approx(0.8)  # 0.2 is inside the window


def test_implausible_mirror_is_rejected_and_next_endpoint_is_tried(
    config: GisConfig, cache: RawCache
) -> None:
    """The overpass.osm.ch failure mode measured on 2026-10-01, reproduced exactly."""
    broken = overpass_payload(
        [{"type": "count", "id": 0, "tags": {"nodes": "0", "ways": "0", "relations": "0", "total": "0"}}],
        timestamp="117395",
    )
    client, transport = make_client(
        config,
        cache,
        [(200, broken), (200, count_payload(12_206))],
        sleeper=SleepRecorder(),
    )
    result = client.fetch(QUERY, label="mirror")
    assert result.record.endpoint == "https://backup.invalid/api/interpreter"
    assert result.json()["elements"][0]["tags"]["ways"] == "12206"
    outcomes = [attempt.outcome for attempt in client.attempts]
    assert "implausible_body" in outcomes
    assert len(transport.requests) == 2
    client.close()


def test_zero_result_guard_refuses_a_silent_empty_answer(config: GisConfig, cache: RawCache) -> None:
    client, _ = make_client(
        config,
        cache,
        [(200, count_payload(0)), (200, count_payload(0))],
        sleeper=SleepRecorder(),
    )
    # Neither endpoint can be believed, so the query fails instead of recording "no
    # buildings in Chandigarh" as a measurement.
    with pytest.raises(EndpointUnavailableError):
        client.fetch(QUERY, label="zero", min_expected_total=1000)
    assert [attempt.outcome for attempt in client.attempts] == ["implausible_body"] * 2
    client.close()


def test_every_response_is_cached_keyed_by_query_hash_and_timestamp(
    config: GisConfig, cache: RawCache
) -> None:
    client, _ = make_client(config, cache, [(200, count_payload(7))], sleeper=SleepRecorder())
    result = client.fetch(QUERY, label="cached")
    key = query_sha256(QUERY)
    path = result.record.path
    assert f"{key}" in path
    assert re.search(r"\d{8}T\d{6}Z\+", Path(path).name), Path(path).name
    assert result.record.response_sha256.startswith("sha256:")
    assert cache.verify(result.record) == result.record.response_sha256
    index = (config.raw_dir / "overpass" / "index.jsonl").read_text(encoding="utf-8")
    assert key in index
    client.close()


def test_whitespace_reformatting_of_a_query_hits_the_same_cache_entry(
    config: GisConfig, cache: RawCache
) -> None:
    assert query_sha256("a  b\n c") == query_sha256("a b c")
    client, transport = make_client(config, cache, [(200, count_payload(3))], sleeper=SleepRecorder())
    client.fetch("nwr[building](1,2,3,4);out count;", label="norm")
    before = len(transport.requests)
    offline = make_client(config, cache, [], live=False, sleeper=SleepRecorder())[0]
    reformatted = "nwr[building](1,2,3,4);\n\n  out  count;\n"
    result = offline.fetch(reformatted, label="norm")
    assert result.source_mode == "cache"
    assert len(transport.requests) == before
    client.close()
    offline.close()


def test_unreachable_endpoint_degrades_to_cache_and_says_so(
    config: GisConfig, cache: RawCache
) -> None:
    client, _ = make_client(config, cache, [(200, count_payload(42))], sleeper=SleepRecorder())
    client.fetch(QUERY, label="first")
    client.close()

    failing, _ = make_client(
        config,
        cache,
        [
            httpx.ConnectError("no route to host"),
            httpx.ConnectError("no route to host"),
            httpx.ConnectError("no route to host"),
            httpx.ConnectTimeout("timed out"),
            httpx.ConnectTimeout("timed out"),
            httpx.ConnectTimeout("timed out"),
        ],
        sleeper=SleepRecorder(),
    )
    result = failing.fetch(QUERY, label="first")
    assert result.source_mode == "cache"
    assert result.degraded is True
    assert "DEGRADED" in result.note
    assert "not" not in result.note.lower() or "re-retrieved" in result.note.lower()
    assert result.json()["elements"][0]["tags"]["ways"] == "42"
    failing.close()


def test_no_cache_and_no_network_raises_without_fabricating(config: GisConfig, cache: RawCache) -> None:
    client, _ = make_client(
        config,
        cache,
        [httpx.ConnectError("down")] * 6,
        sleeper=SleepRecorder(),
    )
    with pytest.raises(EndpointUnavailableError) as excinfo:
        client.fetch("[out:json];nwr[amenity=theatre](1,2,3,4);out count;", label="nothing")
    message = str(excinfo.value)
    assert "fabricate" in message.lower()
    assert "attempts" in dir(excinfo.value)
    assert excinfo.value.attempts, "the failure must show which endpoints were tried"
    client.close()


def test_offline_mode_without_cache_refuses(config: GisConfig, cache: RawCache) -> None:
    client, _ = make_client(config, cache, [], live=False, sleeper=SleepRecorder())
    with pytest.raises(EndpointUnavailableError, match="fabricat"):
        client.fetch(QUERY, label="missing")
    client.close()


def test_attempt_log_records_every_http_try(config: GisConfig, cache: RawCache) -> None:
    sleeper = SleepRecorder()
    client, _ = make_client(
        config, cache, [(504, "busy"), (200, count_payload(1))], sleeper=sleeper
    )
    result = client.fetch(QUERY, label="log")
    log = result.attempt_log()
    assert [entry["outcome"] for entry in log] == ["retryable_status", "ok"]
    assert log[0]["status"] == 504
    assert log[0]["at"]
    client.close()


def test_cache_store_writes_a_readable_sidecar(config: GisConfig, cache: RawCache) -> None:
    record = cache.store(
        QUERY,
        b'{"elements": []}',
        retrieved_at=RETRIEVED_AT,
        endpoint="https://mock.invalid",
        http_status=200,
        replication_timestamp=GOOD_TIMESTAMP,
        label="sidecar",
    )
    assert json.loads(open(record.meta_path, encoding="utf-8").read())["query_sha256"] == record.key
    assert isinstance(datetime.fromisoformat(record.retrieved_at).tzinfo, timezone)
    assert cache.latest(QUERY, label="sidecar") is not None


def test_client_construction_requires_a_positive_rate_limit(config: GisConfig, cache: RawCache) -> None:
    from dataclasses import replace

    with pytest.raises(Exception, match="hammer"):
        OverpassClient(replace(config.overpass, request_minimum_interval_s=0.01), cache).close()
