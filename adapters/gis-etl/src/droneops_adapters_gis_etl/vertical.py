"""The synthetic vertical model (spec 12 section 6), and the guards that keep it honest.

Input: measured horizontal features from :mod:`.extract`. Output: obstacle features
whose every vertical value is an assumption, each one stamped with the mandatory flags
and an uncertainty band.

The five mandatory flags are::

    obstacle_source: synthetic
    vertical_authority: none
    requires_survey: true
    synthetic: true
    authoritative: false

They are applied to *every* generated obstacle, including one whose height came from a
real OSM ``height`` tag. That is deliberate: a crowd-tagged height is not a survey and
not an approved obstacle datum, so it still has ``vertical_authority: none``. What
differs is ``vertical_evidence``, which records how much evidence there was, and
``uncertainty_margin_m``, which records how little can be trusted.

Nothing in this module may state or imply that a corridor is safe, clear, certified or
authorised; :func:`assert_no_safety_implication` is called on every emitted feature and
re-run over the artefacts on disk by the lint test.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import GisConfig, VerticalModel
from .errors import SyntheticProvenanceError
from .geom import make_feature, round_value, write_json, write_jsonl
from .provenance import assert_synthetic_flags, synthetic_provenance

#: Keys that would read as a clearance or authority claim. There are no such keys in
#: this adapter's output and this set is what keeps it that way.
BANNED_KEYS = frozenset(
    {
        "safe",
        "is_safe",
        "safety",
        "safe_route",
        "safety_verified",
        "clearance_verified",
        "obstacle_free",
        "no_obstacles",
        "clear_airspace",
        "certified",
        "approved",
        "authorised",
        "authorized",
        "flight_legal",
        "permitted",
        "risk_free",
        "protected_volume_cleared",
    }
)
_SAFETY_PHRASE = re.compile(
    r"\b(safe|safest|safety|clearance[- ]verified|obstacle[- ]free|clear airspace|certified|"
    r"authori[sz]ed for flight|risk[- ]free|guaranteed)\b",
    re.IGNORECASE,
)
#: A phrase that asserts safety is banned; a phrase that *denies* it is required.
_NEGATIONS = ("not", "never", "no ", "non-", "without", "cannot", "void")

HEIGHT_BASIS_TAGGED = "osm_tagged_height"
HEIGHT_BASIS_LEVELS = "osm_levels_times_storey_height"
HEIGHT_BASIS_RESIDENTIAL = "assumed_untagged_residential_low_class"
HEIGHT_BASIS_COMMERCIAL = "assumed_untagged_commercial_medium_class"
HEIGHT_BASIS_DEFAULT = "assumed_untagged_default"
HEIGHT_BASIS_WIRE = "assumed_power_line_clearance"
HEIGHT_BASIS_TOWER = "assumed_tower_class"
HEIGHT_BASIS_MAST = "assumed_mast_class"
HEIGHT_BASIS_TREE = "assumed_tree_class"

EVIDENCE_GRADE = {
    HEIGHT_BASIS_TAGGED: "osm_tag",
    HEIGHT_BASIS_LEVELS: "osm_tag",
    HEIGHT_BASIS_RESIDENTIAL: "assumption",
    HEIGHT_BASIS_COMMERCIAL: "assumption",
    HEIGHT_BASIS_DEFAULT: "assumption",
    HEIGHT_BASIS_WIRE: "assumption",
    HEIGHT_BASIS_TOWER: "assumption",
    HEIGHT_BASIS_MAST: "assumption",
    HEIGHT_BASIS_TREE: "assumption",
}


@dataclass
class ObstacleSet:
    """Generated obstacles plus the summary the manifest quotes."""

    features: list[dict[str, Any]] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)
    files: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"summary": self.summary, "files": self.files}


def build_obstacles(
    config: GisConfig,
    *,
    buildings: Sequence[dict[str, Any]],
    power_lines: Sequence[dict[str, Any]],
    towers: Sequence[dict[str, Any]],
    trees: Sequence[dict[str, Any]],
) -> ObstacleSet:
    """Turn measured footprints into *assumed* vertical obstacles."""
    model = config.vertical
    features: list[dict[str, Any]] = []
    for feature in buildings:
        features.append(_building_obstacle(config, model, feature))
    for feature in power_lines:
        features.append(_wire_obstacle(config, model, feature))
    for feature in towers:
        features.append(_vertical_structure_obstacle(config, model, feature))
    for feature in trees:
        features.append(_tree_obstacle(config, model, feature))
    for feature in features:
        stamp_synthetic_flags(feature, config)
        audit_feature(feature)

    summary = _summarise(features, model)
    return ObstacleSet(features=features, summary=summary)


# ------------------------------------------------------------------ rule application


def _building_obstacle(
    config: GisConfig, model: VerticalModel, feature: dict[str, Any]
) -> dict[str, Any]:
    props = feature.get("properties", {})
    tags = props.get("tags", {})
    building_class = str(tags.get("building", "yes"))
    tagged_height = props.get("height_tag_m")
    levels = props.get("levels_tag")
    unknown_area = False

    if tagged_height is not None:
        height = float(tagged_height)
        basis = HEIGHT_BASIS_TAGGED
        rule = f"tagged height {tags.get('height')!r} used verbatim"
    elif levels is not None:
        height = float(levels) * model.metres_per_level
        basis = HEIGHT_BASIS_LEVELS
        rule = f"{levels} levels x {model.metres_per_level} m per storey"
    elif building_class in model.residential_buildings:
        height = model.untagged_residential_height_m
        basis = HEIGHT_BASIS_RESIDENTIAL
        rule = f"untagged residential low class ({model.untagged_residential_height_m} m)"
    elif building_class in model.commercial_buildings:
        height = model.untagged_commercial_height_m
        basis = HEIGHT_BASIS_COMMERCIAL
        rule = f"untagged commercial medium class ({model.untagged_commercial_height_m} m)"
    else:
        height = model.untagged_default_height_m
        basis = HEIGHT_BASIS_DEFAULT
        unknown_area = True
        rule = (
            f"untagged building of unknown class, default {model.untagged_default_height_m} m "
            f"plus the unknown-area margin ({model.unknown_area_extra_margin_m} m)"
        )

    return _obstacle(
        config,
        feature,
        obstacle_kind="building",
        height_m=height,
        basis=basis,
        rule=rule,
        unknown_area=unknown_area,
        extra={"building_class": building_class, "levels_tag": levels, "height_tag_m": tagged_height},
    )


def _wire_obstacle(
    config: GisConfig, model: VerticalModel, feature: dict[str, Any]
) -> dict[str, Any]:
    props = feature.get("properties", {})
    tags = props.get("tags", {})
    return _obstacle(
        config,
        feature,
        obstacle_kind="wire",
        height_m=model.power_line_assumed_height_m,
        basis=HEIGHT_BASIS_WIRE,
        rule=(
            f"configured assumed wire clearance {model.power_line_assumed_height_m} m; sag, "
            "temperature, span and pole height are not represented"
        ),
        unknown_area=True,
        extra={
            "power": tags.get("power", ""),
            "voltage": tags.get("voltage", ""),
            "circuits": tags.get("circuits", ""),
            "wire_note": (
                "A power line is a linear obstacle at an unknown height between its supports. "
                "The assumed clearance is a placeholder for a survey, not a physical value: "
                "sag increases with load and temperature and a span's lowest point is not the "
                "support height."
            ),
        },
    )


def _vertical_structure_obstacle(
    config: GisConfig, model: VerticalModel, feature: dict[str, Any]
) -> dict[str, Any]:
    props = feature.get("properties", {})
    tags = props.get("tags", {})
    tagged = props.get("height_tag_m")
    man_made = str(tags.get("man_made", ""))
    if tagged is not None:
        return _obstacle(
            config,
            feature,
            obstacle_kind="tower",
            height_m=float(tagged),
            basis=HEIGHT_BASIS_TAGGED,
            rule=f"tagged height {tags.get('height')!r} used verbatim",
            unknown_area=False,
            extra={"man_made": man_made, "tower_type": tags.get("tower_type", "")},
        )
    mast_like = man_made in {"mast", "communication_tower", "flagpole", "chimney", "minaret"}
    assumed = (
        model.mast_assumed_height_m if mast_like else model.tower_assumed_height_m
    )
    return _obstacle(
        config,
        feature,
        obstacle_kind="tower",
        height_m=assumed,
        basis=HEIGHT_BASIS_MAST if mast_like else HEIGHT_BASIS_TOWER,
        rule=f"assumed {man_made or 'tower'} class height {assumed} m",
        unknown_area=True,
        extra={"man_made": man_made, "tower_type": tags.get("tower_type", "")},
    )


def _tree_obstacle(
    config: GisConfig, model: VerticalModel, feature: dict[str, Any]
) -> dict[str, Any]:
    props = feature.get("properties", {})
    tags = props.get("tags", {})
    tagged = props.get("height_tag_m")
    if tagged is not None:
        return _obstacle(
            config,
            feature,
            obstacle_kind="tree",
            height_m=float(tagged),
            basis=HEIGHT_BASIS_TAGGED,
            rule=f"tagged height {tags.get('height')!r} used verbatim",
            unknown_area=False,
            extra={"natural": tags.get("natural", "")},
        )
    return _obstacle(
        config,
        feature,
        obstacle_kind="tree",
        height_m=model.tree_assumed_height_m,
        basis=HEIGHT_BASIS_TREE,
        rule=f"assumed tree class height {model.tree_assumed_height_m} m",
        unknown_area=True,
        extra={"natural": tags.get("natural", "")},
    )


def _obstacle(
    config: GisConfig,
    feature: dict[str, Any],
    *,
    obstacle_kind: str,
    height_m: float,
    basis: str,
    rule: str,
    unknown_area: bool,
    extra: dict[str, Any],
) -> dict[str, Any]:
    model = config.vertical
    capped = height_m > model.height_cap_m
    height = min(height_m, model.height_cap_m)
    margin = height * model.uncertainty_ratio
    if unknown_area:
        margin += model.unknown_area_extra_margin_m
    source_props = feature.get("properties", {})
    properties: dict[str, Any] = {
        "obstacle_id": f"{obstacle_kind}:{source_props.get('osm_type')}-{source_props.get('osm_id')}",
        "obstacle_kind": obstacle_kind,
        "assumed_height_m": round(height, 3),
        "height_basis": basis,
        "height_rule": rule,
        "vertical_evidence": EVIDENCE_GRADE[basis],
        "uncertainty_margin_m": round(margin, 3),
        "height_upper_bound_m": round(min(height + margin, model.height_cap_m * 2), 3),
        "unknown_area": unknown_area,
        "height_capped": capped,
        "altitude_reference": model.altitude_reference,
        "horizontal_source": "measured_osm",
        "osm_type": source_props.get("osm_type"),
        "osm_id": source_props.get("osm_id"),
        "layer": source_props.get("layer"),
        "name": source_props.get("name", ""),
        "obstacle_vertical_model_version": model.version,
        "clearance_use": "forbidden",
        "clearance_use_note": (
            "This height must not be used to establish obstacle clearance. It exists so a "
            "simulator has something to plan against, and so the map can show where real "
            "data is missing."
        ),
    }
    if capped:
        properties["capping_note"] = (
            f"computed {round(height_m, 2)} m exceeded the configured cap "
            f"{model.height_cap_m} m; the cap is an experiment parameter, and the true "
            "value is unresolved rather than bounded."
        )
    properties.update({k: v for k, v in extra.items() if v not in (None, "")})
    properties["provenance"] = synthetic_provenance(
        derived_from=_rehydrate(source_props.get("provenance") or {}),
        model_version=model.version,
    )
    return make_feature(feature.get("geometry"), properties)


def stamp_synthetic_flags(feature: dict[str, Any], config: GisConfig) -> None:
    """Write the five mandatory flags at the *top level* of the properties.

    They are also inside ``provenance``. Duplicated on purpose: a consumer that reads
    only the flat property bag - which is most of them - still cannot miss them.
    """
    properties = feature["properties"]
    properties["obstacle_source"] = "synthetic"
    properties["vertical_authority"] = "none"
    properties["requires_survey"] = True
    properties["synthetic"] = True
    properties["authoritative"] = False
    properties["altitude_reference"] = config.vertical.altitude_reference
    properties["simulation_only"] = True


def audit_feature(feature: dict[str, Any]) -> None:
    """Fail the build if this obstacle could be mistaken for measured or safe data."""
    properties = feature.get("properties") or {}
    identifier = str(properties.get("obstacle_id", ""))
    assert_synthetic_flags(properties, identifier)
    if feature.get("geometry") is None:
        raise SyntheticProvenanceError(f"obstacle {identifier} has no geometry")
    for key in properties:
        if str(key).lower() in BANNED_KEYS:
            raise SyntheticProvenanceError(
                f"obstacle {identifier} carries the property {key!r}, which reads as a "
                "clearance or authority claim. No such field may be emitted."
            )
    assert_no_safety_implication(properties, identifier)


def assert_no_safety_implication(properties: dict[str, Any], identifier: str) -> None:
    """Reject any value that *asserts* safety; deny-lists the phrase, allows its negation."""
    for key, value in properties.items():
        if isinstance(value, str):
            for match in _SAFETY_PHRASE.finditer(value):
                window = value[max(0, match.start() - 40) : match.end() + 40].lower()
                if not any(negation in window for negation in _NEGATIONS):
                    raise SyntheticProvenanceError(
                        f"obstacle {identifier} property {key!r} says {match.group(0)!r} "
                        "without negating it. A synthetic model may not describe anything "
                        "as safe, clear, certified or guaranteed."
                    )
        elif isinstance(value, dict):
            assert_no_safety_implication(value, identifier)


def _rehydrate(raw: dict[str, Any]) -> Any:
    """Rebuild the retrieval provenance a feature was stamped with, for its obstacle."""
    from .provenance import RetrievalProvenance

    payload = dict(raw)
    payload.setdefault("source", "OpenStreetMap via Overpass API")
    payload.setdefault("retrieved_at", "1970-01-01T00:00:00+00:00")
    for key in ("source", "retrieved_at", "endpoint", "query_sha256", "query_text"):
        payload.setdefault(key, "")
    return RetrievalProvenance.model_validate(payload)


def _summarise(features: Iterable[dict[str, Any]], model: VerticalModel) -> dict[str, Any]:
    basis_counts: dict[str, int] = {}
    kind_counts: dict[str, int] = {}
    flagged = 0
    for feature in features:
        props = feature["properties"]
        basis_counts[str(props["height_basis"])] = basis_counts.get(str(props["height_basis"]), 0) + 1
        kind = str(props["obstacle_kind"])
        kind_counts[kind] = kind_counts.get(kind, 0) + 1
        try:
            assert_synthetic_flags(props, str(props.get("obstacle_id", "")))
        except SyntheticProvenanceError:
            continue
        flagged += 1
    return {
        "obstacles": sum(kind_counts.values()),
        "by_kind": dict(sorted(kind_counts.items())),
        "by_height_basis": dict(sorted(basis_counts.items())),
        "fully_flagged": flagged,
        "vertical_model_version": model.version,
        "assumption_parameters": {
            "metres_per_level": model.metres_per_level,
            "untagged_residential_height_m": model.untagged_residential_height_m,
            "untagged_commercial_height_m": model.untagged_commercial_height_m,
            "untagged_default_height_m": model.untagged_default_height_m,
            "power_line_assumed_height_m": model.power_line_assumed_height_m,
            "unknown_area_extra_margin_m": model.unknown_area_extra_margin_m,
            "uncertainty_ratio": model.uncertainty_ratio,
            "height_cap_m": model.height_cap_m,
            "config_version_key": "configs/gis/chandigarh-synthetic-v1.yaml",
        },
        "statement": (
            "Every height in this file is an assumption or a crowd tag. None of them is a "
            "surveyed or approved obstacle datum, and none may be used to claim clearance."
        ),
    }


def write_obstacles(obstacles: ObstacleSet, jsonl_path: Path, summary_path: Path) -> dict[str, Any]:
    """Persist the obstacle layer and its summary, returning the file hashes."""
    digest = write_jsonl(jsonl_path, obstacles.features)
    summary_digest = write_json(
        summary_path, {**obstacles.summary, "obstacles": len(obstacles.features)}
    )
    return {
        "obstacles_jsonl": {"path": jsonl_path.name, "sha256": digest, "features": len(obstacles.features)},
        "obstacles_summary": {"path": summary_path.name, "sha256": summary_digest},
    }
