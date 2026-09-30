"""Overpass QL builders: the measured coverage metrics and the feature layers.

Two things are generated here and they are deliberately different:

*metrics* are ``out count`` queries whose *numbers* are the evidence. They are cheap,
they are re-runnable, and they are what makes "OSM has no vertical data in Chandigarh"
a current measurement instead of a quoted claim.

*layers* are geometry queries turned into normalised GeoJSON/JSONL. They are chunked
because a public endpoint that 504s on a 12 x 15 km bbox answers the same request
split into four boxes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import Bbox, GisConfig
from .errors import QueryError

# --------------------------------------------------------------------------- metrics


@dataclass(frozen=True)
class Metric:
    """One measured coverage number and the 30 September baseline it is compared to."""

    name: str
    description: str
    bbox: Bbox
    filters: tuple[str, ...]
    baseline_key: str

    @property
    def bbox_fragment(self) -> str:
        return self.bbox.as_overpass()

    def query(self, timeout_s: int = 25, *, bbox: Bbox | None = None) -> str:
        box = (bbox or self.bbox).as_overpass()
        blocks = "\n  ".join(f"{filter_}({box});" for filter_ in self.filters)
        if len(self.filters) > 1:
            body = f"(\n  {blocks}\n);"
        else:
            body = blocks
        return f"[out:json][timeout:{timeout_s}];\n{body}\nout count;"


def service_metrics(bbox: Bbox) -> list[Metric]:
    """The count set measured on 30 September 2026, re-measured on the same shape."""
    specs = [
        ("building_ways", "building ways in the service bbox", ("nwr[building]",), "building_ways"),
        (
            "tagged_height",
            'building ways tagged "height"',
            ('nwr["height"~"."]["building"]',),
            "tagged_height",
        ),
        (
            "tagged_building_levels",
            'building ways tagged "building:levels"',
            ('nwr["building:levels"~"."]["building"]',),
            "tagged_building_levels",
        ),
        ("power_towers", "power towers", ("nwr[power=tower]",), "power_towers"),
        ("power_lines", "power lines (ways)", ("way[power=line]",), "power_lines"),
        ("highway_ways", "highway ways", ("way[highway]",), "highway_ways"),
        ("aeroway_ways", "aeroway ways", ("way[aeroway]",), "aeroway_ways"),
        ("man_made_towers", "man-made towers", ("nwr[man_made=tower]",), "man_made_towers"),
        ("trees", "trees and tree rows", ('nwr["natural"~"^(tree|tree_row)$"]',), "trees"),
    ]
    metrics = [
        Metric(name, description, bbox, filters, baseline_key)
        for name, description, filters, baseline_key in specs
    ]
    return metrics + _extra_metrics(bbox)


def _extra_metrics(bbox: Bbox) -> list[Metric]:
    """Numbers the spec did not ask for but that decide what the ETL can claim."""
    return [
        Metric(
            "masts",
            "man-made masts and communication towers",
            bbox,
            ('nwr["man_made"~"^(mast|communication_tower|telephone_exchange)$"]',),
            "masts",
        ),
        Metric(
            "power_poles",
            "power poles and catenary masts",
            bbox,
            ('nwr["power"~"^(pole|catenary_mast)$"]',),
            "power_poles",
        ),
        Metric(
            "buildings_with_any_vertical_tag",
            "buildings with height OR building:levels: the real vertical coverage ceiling",
            bbox,
            ('nwr["height"~"."]["building"]', 'nwr["building:levels"~"."]["building"]'),
            "buildings_with_any_vertical_tag",
        ),
    ]


def central_metrics(bbox: Bbox) -> list[Metric]:
    """The tighter central query: 302 buildings, none with height, on 30 September."""
    return [
        Metric("central_buildings", "buildings in the central box", bbox, ("nwr[building]",), "central_buildings"),
        Metric(
            "central_with_height",
            'central buildings tagged "height"',
            bbox,
            ('nwr["height"~"."]["building"]',),
            "central_with_height",
        ),
        Metric(
            "central_with_levels",
            'central buildings tagged "building:levels"',
            bbox,
            ('nwr["building:levels"~"."]["building"]',),
            "central_with_levels",
        ),
    ]


# ----------------------------------------------------------------------------- layers


def boundary_query(relation_id: int, *, timeout_s: int = 60) -> str:
    """The administrative relation with member geometry, so a polygon can be rebuilt."""
    return f"[out:json][timeout:{timeout_s}];\nrel(id:{relation_id});\nout geom;"


def boundary_tags_query(relation_id: int, *, timeout_s: int = 30) -> str:
    """Identity check only. A boundary we cannot name is not a boundary we can publish."""
    return f"[out:json][timeout:{timeout_s}];\nrel(id:{relation_id});\nout tags;"


LAYER_FILTERS: dict[str, tuple[str, ...]] = {
    "buildings": ("nwr[building]",),
    "landuse": ("nwr[landuse]",),
    "pois": (
        'nwr["amenity"~"^(hospital|clinic|laboratory|pharmacy|doctors|dentist)$"]',
        'nwr["healthcare"~"."]',
        'nwr["amenity"~"^(university|college|school|kindergarten|research_institute)$"]',
        'nwr["craft"~"laboratory"]',
        'nwr["office"~"^(research|laboratory)$"]',
    ),
    "towers": (
        'nwr["man_made"~"^(tower|mast|communication_tower|silo|chimney|minaret|water_tower|flagpole)$"]',
        'nwr["communication"~"."]',
    ),
    "power_lines": ('nwr["power"~"^(line|minor_line|cable)$"]',),
    "power_minor": ('nwr["power"~"^(pole|transformer|substation|generator|insulator|switch)$"]',),
    "water": (
        'nwr["waterway"~"^(river|stream|canal|drain|ditch)$"]',
        "nwr[natural=water]",
        "nwr[waterway=dam]",
        "nwr[landuse=reservoir]",
    ),
    "parks": (
        'nwr["leisure"~"^(park|garden|recreation_ground|playground|green_space|village_green|dog_park|sports_centre)$"]',
        "nwr[landuse=greenfield]",
        "nwr[boundary=national_park]",
        "nwr[natural=wood]",
    ),
    "aeroway": ("nwr[aeroway]",),
    "trees": ('nwr["natural"~"^(tree|tree_row)$"]',),
    "roads": ("way[highway]",),
}


def layer_query(
    name: str,
    bbox: Bbox,
    *,
    output: str = "center",
    timeout_s: int = 60,
    config: GisConfig | None = None,
) -> str:
    """Build one chunk's layer query.

    ``roads`` takes its class filter from the corridor config rather than fetching all
    ~50k highway ways: a corridor heuristic needs the classified network, and pulling
    every track and footpath off a volunteer endpoint to guess it is not polite.
    """
    filters = LAYER_FILTERS.get(name)
    if not filters:
        raise QueryError(f"no Overpass filter defined for layer {name!r}")
    if output not in {"center", "geom"}:
        raise QueryError(f"unknown layer output {output!r}")
    box = bbox.as_overpass()
    if name == "roads" and config is not None:
        classes = _road_classes(config)
        pattern = "|".join(sorted(classes))
        filters = (f'way["highway"~"^({pattern})$"]',)
    blocks = "\n  ".join(f"{filter_}({box});" for filter_ in filters)
    body = f"(\n  {blocks}\n);" if len(filters) > 1 else blocks
    return f"[out:json][timeout:{timeout_s}];\n{body}\nout {output};"


def _road_classes(config: GisConfig) -> tuple[str, ...]:
    configured = config.corridor.get("road_classes", [])
    cycle = config.raw.get("layers", {}).get("roads", {}).get("cycle_classes", [])
    extra = list(cycle) or ["cycleway", "footway", "path", "pedestrian"]
    classes = {str(item) for item in configured} | {str(item) for item in extra}
    if not classes:
        raise QueryError("corridor.road_classes is empty: no network to derive corridors from")
    return tuple(sorted(classes))


def layer_names(config: GisConfig) -> list[str]:
    return [name for name, spec in sorted(config.layers.items()) if spec.enabled and name != "boundary"]


def layer_settings(config: GisConfig, name: str) -> dict[str, Any]:
    return dict(config.raw.get("layers", {}).get(name, {}))
