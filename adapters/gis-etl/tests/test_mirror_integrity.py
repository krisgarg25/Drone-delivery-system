"""The untrustworthy-mirror tests: a 200 that lies is a failure, not a fact.

Measured on 2026-09-30 while building this adapter: one public mirror answers HTTP 200
with a well-formed JSON body, a plausible-looking ``timestamp_osm_base`` and *nothing in
it* for every Chandigarh query. Two distinct defects follow, and both are tested here,
because accepting either one turns a retrieval failure into fake evidence:

1. an empty response is stored and later replayed offline as if it were data;
2. a response whose elements carry geometry that yields no usable features is written
   out as an empty layer file, which reads as "Chandigarh contains no buildings".

The client rejects case 1 by plausibility floor, the extractor rejects case 2 at layer
level, and both bodies are quarantined: kept on disk as evidence *about the mirror*, in
a directory the replay path never looks in.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from droneops_adapters_gis_etl.cache import QUARANTINE_DIRNAME, RawCache
from droneops_adapters_gis_etl.config import CONFIG_RELPATH, GisConfig, load_config
from droneops_adapters_gis_etl.errors import EndpointUnavailableError, ImplausibleEndpointError
from droneops_adapters_gis_etl.extract import LayerExtractor
from droneops_adapters_gis_etl.geom import boundary_polygon
from droneops_adapters_gis_etl.overpass import OverpassClient
from droneops_adapters_gis_etl.pipeline import ChandigarhEtl

from gis_etl_support import (
    MockTransport,
    building_node,
    config_template,
    overpass_payload,
    road_way,
)

BUILDINGS_QUERY = (
    "[out:json][timeout:25];\nnwr[building](30.680000,76.720000,30.780000,76.860000);\nout center;"
)


def _client(
    config: GisConfig,
    cache: RawCache,
    answers: list[Any],
    *,
    live: bool = True,
) -> tuple[OverpassClient, MockTransport]:
    transport = MockTransport(answers)
    client = OverpassClient(
        config.overpass,
        cache,
        live=live,
        transport=httpx.MockTransport(transport.handler),
        sleeper=lambda seconds: None,
    )
    return client, transport


def _square_mask() -> Any:
    """A boundary polygon around 30.74/76.79, the coordinate the fixtures use."""
    from shapely.geometry import Polygon

    ring = [(30.70, 76.75), (30.70, 76.83), (30.78, 76.83), (30.78, 76.75)]
    return Polygon([(lon, lat) for lat, lon in ring])


# ------------------------------------------------------------------ case 1: silent zero


def test_empty_but_plausible_body_is_rejected_and_quarantined(
    config: GisConfig, cache: RawCache
) -> None:
    """HTTP 200 + valid timestamp + zero elements must not become a measurement."""
    lying = overpass_payload([])
    honest = overpass_payload([building_node(1, tags={})])
    client, transport = _client(config, cache, [(200, lying), (200, honest)])
    result = client.fetch(BUILDINGS_QUERY, label="layer:buildings:chunk0", min_expected_total=1)

    assert result.source_mode == "live"
    assert result.record.endpoint == "https://backup.invalid/api/interpreter"
    assert "implausible_body" in [attempt.outcome for attempt in client.attempts]
    assert len(transport.requests) == 2
    client.close()

    quarantined = cache.quarantined()
    assert len(quarantined) == 1, quarantined
    entry = quarantined[0]
    assert entry["endpoint"] == "https://mock.invalid/api/interpreter"
    assert entry["quarantined"] is True
    assert "result total of 0" in str(entry["reason"])
    body = Path(str(entry["path"]))
    assert body.is_file() and QUARANTINE_DIRNAME in body.as_posix()
    assert json.loads(body.read_text(encoding="utf-8"))["elements"] == []
    index_lines = [
        json.loads(line)
        for line in (config.raw_dir / "overpass" / "index.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    refused = [line for line in index_lines if line["source_mode"] == "quarantined"]
    assert len(refused) == 1 and refused[0]["endpoint"] == "https://mock.invalid/api/interpreter"


def test_a_quarantined_zero_is_never_replayed_as_cache(
    config: GisConfig, cache: RawCache
) -> None:
    """The rejection must survive an offline run: quarantine is not a cache entry."""
    client, _ = _client(config, cache, [(200, overpass_payload([]))])
    with pytest.raises(EndpointUnavailableError):
        client.fetch(BUILDINGS_QUERY, label="layer:buildings:chunk0", min_expected_total=1)
    assert len(cache.quarantined()) == 1
    assert cache.latest(BUILDINGS_QUERY, label="layer:buildings:chunk0") is None
    client.close()

    offline, _ = _client(config, cache, [], live=False)
    with pytest.raises(EndpointUnavailableError):
        offline.fetch(BUILDINGS_QUERY, label="layer:buildings:chunk0")
    offline.close()


def test_stale_database_timestamp_is_refused(config: GisConfig, cache: RawCache) -> None:
    """``timestamp_osm_base`` "117395" - the exact value the broken mirror reported."""
    broken = overpass_payload(
        [{"type": "count", "id": 0, "tags": {"nodes": "0", "ways": "0", "total": "0"}}],
        timestamp="117395",
    )
    client, _ = _client(
        config,
        cache,
        [
            (200, broken),
            (200, overpass_payload([], timestamp="0001-01-01 00:00:00")),
        ],
    )
    with pytest.raises(EndpointUnavailableError):
        client.fetch(BUILDINGS_QUERY, label="metric", min_expected_total=10)
    assert len(cache.quarantined()) == 2
    assert "timestamp_osm_base" in str(cache.quarantined()[0]["reason"])
    client.close()


# ------------------------------------------------------------------ case 2: zero features


def test_layer_with_elements_but_no_usable_features_is_refused(config: GisConfig) -> None:
    """The gap the response-level floor cannot see: geometry that yields nothing."""
    cache = RawCache(config.raw_dir / "overpass", namespace="")
    # 17 well-formed building nodes, all of them hundreds of kilometres outside the mask.
    outside = overpass_payload(
        [
            building_node(1000 + index, tags={}, lat=29.0 + index / 100, lon=75.0 + index / 100)
            for index in range(17)
        ]
    )
    client, _ = _client(config, cache, [(200, outside)])
    extractor = LayerExtractor(config, client)
    with pytest.raises(ImplausibleEndpointError, match="0 usable features"):
        extractor.run_layer("buildings", _square_mask())
    client.close()
    assert not (config.processed_dir / "layers" / "buildings.jsonl").exists()


def test_a_genuine_empty_subchunk_is_still_accepted(config: GisConfig) -> None:
    """A corner of the grid really can be empty: the floor must not invent a failure."""
    template = config_template()
    template["layers"]["buildings"]["chunk_grid"] = 2
    target = config.root / CONFIG_RELPATH
    target.write_text(yaml.safe_dump(template, sort_keys=False), encoding="utf-8")
    chunked = load_config(target, root=config.root)
    chunked.processed_dir.mkdir(parents=True, exist_ok=True)
    cache = RawCache(chunked.raw_dir / "overpass", namespace="")
    client, _ = _client(
        chunked,
        cache,
        [
            (200, overpass_payload([building_node(7, tags={})])),
            (200, overpass_payload([])),
            (200, overpass_payload([])),
            (200, overpass_payload([])),
        ],
    )
    outcome = LayerExtractor(chunked, client).run_layer("buildings", _square_mask())
    assert outcome.features == 1, outcome.as_dict()
    assert outcome.chunks_requested == 4
    assert (chunked.processed_dir / "layers" / "buildings.jsonl").is_file()
    assert cache.quarantined() == []
    client.close()


# --------------------------------------------------------------------- pipeline behaviour


def test_pipeline_records_a_refused_layer_and_keeps_running(config: GisConfig) -> None:
    """A refusal is a result the manifest can carry, not a crash that loses the run."""
    # The cooldown is zeroed so one lying layer does not hide the mirror state of the
    # next: what is under test here is the pipeline's handling of a refusal, and the
    # cooldown behaviour itself has its own tests above.
    template = config_template()
    template["overpass"]["endpoint_cooldown_s"] = 0.0
    target = config.root / CONFIG_RELPATH
    target.write_text(yaml.safe_dump(template, sort_keys=False), encoding="utf-8")
    reloaded = load_config(target, root=config.root)
    reloaded.processed_dir.mkdir(parents=True, exist_ok=True)

    transport = MockTransport(
        [
            (200, overpass_payload([])),
            (200, overpass_payload([])),
            (200, overpass_payload([road_way(11), road_way(12, offset=0.001)])),
        ]
    )
    etl = ChandigarhEtl(
        reloaded,
        live=True,
        transport=httpx.MockTransport(transport.handler),
        sleeper=lambda seconds: None,
    )
    try:
        etl.run_layers(["buildings", "roads"])
    finally:
        etl.close()

    assert etl.record.layers["buildings"]["refused"] is True
    assert etl.record.layers["buildings"]["features"] == 0
    assert "REFUSED" in " ".join(etl.record.degradations)
    assert etl.record.layers["roads"]["features"] == 2
    assert not (reloaded.processed_dir / "layers" / "buildings.jsonl").exists()
    assert (reloaded.processed_dir / "layers" / "roads.jsonl").is_file()


def test_boundary_polygon_reads_relation_member_geometry() -> None:
    """`out geom` on a relation carries the perimeter on `members`, not on the relation.

    Measured against relation 1942809 on 2026-09-30: 1 element, 40 members, no
    relation-level ``geometry``. Reading only the top level polygonises nothing and the
    run dies with "no ring could be polygonised" on a perfectly good boundary.
    """
    perimeter = [
        (30.70, 76.75),
        (30.70, 76.83),
        (30.78, 76.83),
        (30.78, 76.75),
    ]
    members: list[dict[str, Any]] = [
        {"type": "node", "ref": 1, "role": "admin_centre", "lat": 30.73, "lon": 76.78}
    ]
    for index in range(4):
        start, end = perimeter[index], perimeter[(index + 1) % 4]
        members.append(
            {
                "type": "way",
                "ref": 100 + index,
                "role": "outer",
                "geometry": [
                    {"lat": start[0], "lon": start[1]},
                    {"lat": (start[0] + end[0]) / 2, "lon": (start[1] + end[1]) / 2},
                    {"lat": end[0], "lon": end[1]},
                ],
            }
        )
    relation = {"type": "relation", "id": 1942809, "tags": {"name": "Chandigarh"}, "members": members}
    polygon = boundary_polygon([relation])
    assert not polygon.is_empty
    assert polygon.is_valid
    assert polygon.area > 0.005
