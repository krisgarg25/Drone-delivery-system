"""The synthetic vertical model: every rule in spec 12 section 6, and the flags.

These tests are the reason the model can be trusted to mean what it says. Each rule is
checked against a number, and the five mandatory flags are checked on the object that
comes out - not on the input that went in.
"""

from __future__ import annotations

from typing import Any

import pytest
from conftest import RETRIEVED_AT, building_node, wire_way

from droneops_adapters_gis_etl.config import GisConfig
from droneops_adapters_gis_etl.errors import SyntheticProvenanceError
from droneops_adapters_gis_etl.extract import LayerExtractor, parse_height_m, parse_levels
from droneops_adapters_gis_etl.provenance import assert_synthetic_flags
from droneops_adapters_gis_etl.queries import layer_query
from droneops_adapters_gis_etl.vertical import (
    HEIGHT_BASIS_COMMERCIAL,
    HEIGHT_BASIS_DEFAULT,
    HEIGHT_BASIS_LEVELS,
    HEIGHT_BASIS_RESIDENTIAL,
    HEIGHT_BASIS_TAGGED,
    HEIGHT_BASIS_WIRE,
    audit_feature,
    build_obstacles,
)

ALL_FLAGS = {
    "obstacle_source": "synthetic",
    "vertical_authority": "none",
    "requires_survey": True,
    "synthetic": True,
    "authoritative": False,
}


def _feature(config: GisConfig, element: dict[str, Any]) -> dict[str, Any]:
    """Normalise one Overpass element the way the extractor would, provenance included."""
    from droneops_adapters_gis_etl.extract import LayerExtractor
    from droneops_adapters_gis_etl.provenance import RetrievalProvenance
    from droneops_adapters_gis_etl.geom import element_geometry, element_properties, make_feature, round_value

    provenance = RetrievalProvenance(
        source="OpenStreetMap via Overpass API (https://mock.invalid)",
        retrieved_at=RETRIEVED_AT,
        query_sha256="a" * 64,
        response_sha256="sha256:" + "b" * 64,
        raw_path="data/raw/overpass/x.json",
        endpoint="https://mock.invalid",
    )
    properties = element_properties(element, layer="buildings")
    properties["height_tag_m"] = parse_height_m(element.get("tags", {}).get("height"))
    properties["levels_tag"] = parse_levels(element.get("tags", {}).get("building:levels"))
    properties["provenance"] = provenance.as_dict()
    return make_feature(round_value(element_geometry(element) or {}), properties)


def _build(config: GisConfig, elements: list[dict[str, Any]], *, kind: str = "buildings") -> dict[str, Any]:
    features = [_feature(config, element) for element in elements]
    kwargs: dict[str, list[dict[str, Any]]] = {
        "buildings": [],
        "power_lines": [],
        "towers": [],
        "trees": [],
    }
    kwargs[kind] = features
    obstacles = build_obstacles(config, **kwargs)
    assert obstacles.features, "no obstacle produced"
    return obstacles.features[0]


def test_levels_are_multiplied_by_the_configured_storey_height(config: GisConfig) -> None:
    feature = _build(
        config, [building_node(1, tags={"building": "apartment", "building:levels": "4"})]
    )
    assert feature["properties"]["assumed_height_m"] == pytest.approx(4 * 3.2)
    assert feature["properties"]["height_basis"] == HEIGHT_BASIS_LEVELS
    assert "3.2" in feature["properties"]["height_rule"]


def test_tagged_height_is_used_verbatim_but_is_still_not_authority(config: GisConfig) -> None:
    feature = _build(config, [building_node(2, tags={"height": "21"})])
    assert feature["properties"]["assumed_height_m"] == pytest.approx(21.0)
    assert feature["properties"]["height_basis"] == HEIGHT_BASIS_TAGGED
    assert feature["properties"]["vertical_evidence"] == "osm_tag"
    # The point of this test: a tagged height is still vertical_authority none.
    assert feature["properties"]["vertical_authority"] == "none"
    assert feature["properties"]["requires_survey"] is True


def test_untagged_residential_gets_the_low_class(config: GisConfig) -> None:
    feature = _build(config, [building_node(3, tags={"building": "residential"})])
    assert feature["properties"]["assumed_height_m"] == pytest.approx(6.4)
    assert feature["properties"]["height_basis"] == HEIGHT_BASIS_RESIDENTIAL
    assert feature["properties"]["unknown_area"] is False


def test_untagged_commercial_gets_the_medium_class(config: GisConfig) -> None:
    feature = _build(config, [building_node(4, tags={"building": "commercial"})])
    assert feature["properties"]["assumed_height_m"] == pytest.approx(12.8)
    assert feature["properties"]["height_basis"] == HEIGHT_BASIS_COMMERCIAL


def test_unknown_class_gets_the_default_plus_the_uncertainty_margin(config: GisConfig) -> None:
    feature = _build(config, [building_node(5, tags={"building": "yes"})])
    properties = feature["properties"]
    assert properties["height_basis"] == HEIGHT_BASIS_DEFAULT
    assert properties["unknown_area"] is True
    # default 9.6 m, ratio 0.5 -> 4.8, plus the 10 m unknown-area margin
    assert properties["uncertainty_margin_m"] == pytest.approx(9.6 * 0.5 + 10.0)


def test_power_lines_get_the_configured_assumed_clearance(config: GisConfig) -> None:
    feature = _build(config, [wire_way(6)], kind="power_lines")
    properties = feature["properties"]
    assert properties["assumed_height_m"] == pytest.approx(8.0)
    assert properties["obstacle_kind"] == "wire"
    assert properties["height_basis"] == HEIGHT_BASIS_WIRE
    assert "sag" in properties["wire_note"]


def test_every_generated_obstacle_carries_every_flag(config: GisConfig) -> None:
    for element, kind in (
        (building_node(7, tags={"building": "residential"}), "buildings"),
        (wire_way(8), "power_lines"),
    ):
        feature = _build(config, [element], kind=kind)
        for key, expected in ALL_FLAGS.items():
            assert feature["properties"][key] == expected, f"{key} on {kind}"
        assert_synthetic_flags(feature["properties"], "test")


def test_flags_are_duplicated_into_the_provenance_block(config: GisConfig) -> None:
    feature = _build(config, [building_node(9, tags={"building": "house"})])
    provenance = feature["properties"]["provenance"]
    for key, expected in ALL_FLAGS.items():
        assert provenance[key] == expected
    # and the horizontal source is still traceable
    assert provenance["source_query_sha256"] == "a" * 64
    assert provenance["source_raw_path"].startswith("data/raw/")
    assert provenance["source_response_sha256"].startswith("sha256:")
    assert provenance["altitude_reference"] == "AGL"


def test_a_stripped_flag_fails_the_audit(config: GisConfig) -> None:
    feature = _build(config, [building_node(10, tags={"building": "house"})])
    del feature["properties"]["requires_survey"]
    with pytest.raises(SyntheticProvenanceError, match="requires_survey"):
        audit_feature(feature)

    feature2 = _build(config, [building_node(11, tags={"building": "house"})])
    feature2["properties"]["authoritative"] = True
    with pytest.raises(SyntheticProvenanceError, match="authoritative"):
        audit_feature(feature2)


def test_height_cap_marks_the_value_unresolved_instead_of_bounded(config: GisConfig) -> None:
    feature = _build(config, [building_node(12, tags={"building:levels": "40", "building": "roof"})])
    properties = feature["properties"]
    assert properties["assumed_height_m"] == pytest.approx(60.0)
    assert properties["height_capped"] is True
    assert "unresolved" in properties["capping_note"]


def test_altitude_reference_is_explicit_on_every_obstacle(config: GisConfig) -> None:
    feature = _build(config, [building_node(13, tags={"building": "house"})])
    assert feature["properties"]["altitude_reference"] == config.vertical.altitude_reference
    assert feature["properties"]["altitude_reference"] != "UNSPECIFIED"


def test_assumption_parameters_come_from_config_not_code(tmp_path: Any, config_root: Any) -> None:
    """Change the config, the obstacle changes. Nothing about a height is hard-coded."""
    import yaml

    from droneops_adapters_gis_etl.config import load_config

    path = config_root / "configs" / "gis" / "chandigarh-synthetic-v1.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["synthetic_vertical_model"]["untagged_residential_height_m"] = 3.0
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    reloaded = load_config(path, root=config_root)
    assert reloaded.vertical.untagged_residential_height_m == 3.0
    feature = _build(reloaded, [building_node(14, tags={"building": "residential"})])
    assert feature["properties"]["assumed_height_m"] == pytest.approx(3.0)


def test_obstacle_summary_counts_kinds_and_bases(config: GisConfig) -> None:
    obstacles = build_obstacles(
        config,
        buildings=[_feature(config, building_node(15, tags={"building": "residential"}))],
        power_lines=[_feature(config, wire_way(16))],
        towers=[],
        trees=[],
    )
    summary = obstacles.summary
    assert summary["obstacles"] == 2
    assert summary["by_kind"] == {"building": 1, "wire": 1}
    assert summary["fully_flagged"] == 2
    assert "assumption" in summary["statement"].lower()


def test_layer_query_uses_the_configured_road_classes(config: GisConfig) -> None:
    query = layer_query("roads", config.service_bbox.split(1, 1)[0], output="geom", config=config)
    assert '"highway"~"^(' in query
    for road_class in config.corridor["road_classes"]:
        assert road_class in query


def test_extractor_writes_provenance_on_every_feature(config: GisConfig) -> None:
    # A pure check on the helper that builds the property bag: no network, no cache.
    element = building_node(17, tags={"building": "house", "name": "Anon"})
    properties = LayerExtractor(config, client=None)._feature(  # type: ignore[arg-type]
        element, "buildings", _provenance()
    )["properties"]
    assert properties["provenance"]["retrieved_at"].startswith("2026-")
    assert properties["provenance"]["licence"] == "ODbL"
    assert properties["name"] == "Anon"


def _provenance() -> Any:
    from droneops_adapters_gis_etl.provenance import RetrievalProvenance

    return RetrievalProvenance(
        source="OpenStreetMap via Overpass API (https://mock.invalid)",
        retrieved_at=RETRIEVED_AT,
        query_sha256="c" * 64,
        response_sha256="sha256:" + "d" * 64,
        raw_path="data/raw/overpass/y.json",
    )
