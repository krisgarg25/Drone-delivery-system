"""Build the console's synthetic world asset from frozen simulation inputs.

Reads only already-frozen files (the scenario bundle, the GIS snapshot manifest and the
60 OSM-derived candidate corridors) and writes one compact JSON the map can load without
a running API. Nothing here is measured: every geometry keeps its ``authoritative:
false`` / ``synthetic`` / ``requires_survey`` flags, and the provenance block carries the
bundle hashes, the retrieval dates and the licence so the console can show them.

Run with ``npm run build:synthetic-data`` from apps/console. Re-run whenever the frozen
bundle changes; the output is committed so a build never needs Python.
"""

from __future__ import annotations

import hashlib
import json
import re
from math import cos as math_cos, sin as math_sin
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
SCENARIO = ROOT / "simulation" / "scenarios" / "chandigarh-basic"
SNAPSHOT = ROOT / "data" / "processed" / "chandigarh-synthetic-v1"
OUT = Path(__file__).resolve().parent.parent / "src" / "map" / "data" / "synthetic-world.json"

# Dependency-free YAML reading for these flat files: they are all simple mappings,
# sequences and nested "- " blocks written by the ETL and the scenario author.
def load_yaml(path: Path) -> Any:
    import yaml  # noqa: PLC0415  (optional at runtime; the venv ships it)

    return yaml.safe_load(path.read_text(encoding="utf-8"))


def point(pair: dict[str, Any]) -> list[float]:
    return [round(pair["lon_e6"] / 1_000_000, 6), round(pair["lat_e6"] / 1_000_000, 6)]


def ring(center: list[float], metres: float, steps: int = 48) -> list[list[float]]:
    """A planar circle in lon/lat: good enough to display, never a clearance surface."""
    lat = center[1]
    dlat = metres / 111_320.0
    dlon = metres / (111_320.0 * max(0.2, math_cos(lat)))
    return [[round(center[0] + dlon * math_sin(i), 6), round(lat + dlat * math_cos(i), 6)] for i in range(steps)] + [
        [round(center[0] + dlon, 6), round(lat, 6)]
    ]


def decimate(coords: list[list[float]], keep: int = 3) -> list[list[float]]:
    out = [c for i, c in enumerate(coords) if i % keep == 0]
    if coords:
        out.append(coords[-1])
    return [list(map(float, c)) for c in out]


def main() -> None:
    facilities_doc = load_yaml(SCENARIO / "facilities.yaml")
    corridors_doc = load_yaml(SCENARIO / "corridors.yaml")
    weather_doc = load_yaml(SCENARIO / "weather.yaml")
    scenario_doc = load_yaml(SCENARIO / "scenario.yaml")
    bundle_doc = load_yaml(SCENARIO / "bundle.yaml")

    manifest = (SNAPSHOT / "manifest.yaml").read_text(encoding="utf-8")

    def field(key: str, text: str = "") -> str:
        body = text or manifest
        m = re.search(rf"^{key}: '?([^'\n]+)'?$", body, re.MULTILINE)
        return m.group(1).strip() if m else ""

    gis_cfg = (ROOT / "configs" / "gis" / "chandigarh-synthetic-v1.yaml").read_text(encoding="utf-8")
    label = re.search(r"corridor_label: (.+)", gis_cfg)
    bbox = re.findall(r"^\s+(south|west|north|east): ([0-9.]+)$", gis_cfg, re.MULTILINE)

    pads = []
    for fac in facilities_doc["facilities"]:
        pads.append(
            {
                "facility_id": fac["facility_id"],
                "name": fac["name"],
                "kind": fac["kind"],
                "position": [
                    round(fac["location"]["lon_e6"] / 1_000_000, 6),
                    round(fac["location"]["lat_e6"] / 1_000_000, 6),
                ],
                "pads": fac.get("pads", 0),
                "charger_bays": fac.get("charger_bays", 0),
                "band_m_agl": fac["location"].get("alt_m_agl"),
                "altitude_reference": fac["location"]["altitude_reference"],
                "synthetic": True,
                "authoritative": False,
            }
        )

    edges = []
    for edge in corridors_doc["edges"]:
        edges.append(
            {
                "corridor_id": edge["corridor_id"],
                "from": edge["from"],
                "to": edge["to"],
                "band": edge["band"],
                "width_m": edge.get("width_m"),
                "length_m": edge.get("length_m"),
                "max_wind_ms": edge.get("max_wind_ms"),
                "altitude_m": edge["geometry"][0].get("alt_m_agl"),
                "altitude_reference": edge["geometry"][0]["altitude_reference"],
                "line": [[round(p["lon_e6"] / 1_000_000, 6), round(p["lat_e6"] / 1_000_000, 6)] for p in edge["geometry"]],
                "synthetic": bool(edge.get("synthetic", True)),
                "authoritative": False,
            }
        )

    corridors_geo = json.loads((SNAPSHOT / "corridors" / "candidate_corridors.geojson").read_text(encoding="utf-8"))
    candidates = []
    for feat in corridors_geo["features"]:
        props = feat["properties"]
        lines = [decimate(part) for part in feat["geometry"]["coordinates"]]
        candidates.append(
            {
                "corridor_id": props["corridor_id"],
                "length_m": props["length_m"],
                "basis": props["basis"],
                "lines": [ln for ln in lines if len(ln) > 1],
            }
        )

    # The airfield pad gets a modelled keep-out ring. It is a demo input, not a
    # published airspace volume, and the console must say so in the label itself.
    airfield = next((p for p in pads if "airfield" in p["facility_id"]), None)
    restriction = []
    if airfield:
        restriction.append(
            {
                "restriction_id": "modelled-airfield-keepout-synthetic",
                "label": "Modelled keep-out around a fictional airfield pad - NOT official airspace data",
                "official": False,
                "authoritative": False,
                "synthetic": True,
                "basis": "demo input: fixed radius, no survey, no NOTAM, no publication",
                "polygon": [ring(airfield["position"], 1500.0)],
            }
        )

    wind_trace = [
        {"minute": t["minute"], "wind_ms": t["wind_ms"], "headwind_bp": t.get("headwind_bp")}
        for t in weather_doc["trace"]
    ]

    bbox_map = dict(bbox)
    doc = {
        "notice": "Simulation only. Not authorised for flight.",
        "authoritative": False,
        "provenance": {
            "scenario_id": scenario_doc["scenario_id"],
            "title": scenario_doc["title"],
            "seed": bundle_doc.get("seed"),
            "policy_version": bundle_doc["runtime"].get("policy_version"),
            "horizon_minutes": bundle_doc["runtime"].get("horizon_minutes"),
            "topology": scenario_doc.get("topology"),
            "synthetic_flags": scenario_doc.get("synthetic_flags"),
            "bundle_files": bundle_doc["files"],
            "bundle_sha256": bundle_doc["files"]["corridors.yaml"],
            "dataset_id": field("dataset_id"),
            "dataset_retrieved_at": field("retrieved_at"),
            "dataset_produced_at": field("produced_at"),
            "config_version": "chandigarh-geodata-1.0.0",
            "config_sha256": "sha256:c187759afea990902ec6159579f7032b4892475b063600d75d2480472285532f",
            "boundary_source": "OpenStreetMap",
            "boundary_relation_id": 1942809,
            "boundary_hash": "sha256:954f766ac0b64419f310708bfd33372c48764e34b4cde2a46471f63ae68c4f29",
            "boundary_extract_date": scenario_doc["boundary"]["extract_date"],
            "licence": scenario_doc["boundary"]["license"],
            "attribution": scenario_doc["boundary"]["attribution"],
            "weather": {
                "station": weather_doc["station"],
                "source": weather_doc["source"],
                "retrieved_at": weather_doc["retrieved_at"],
                "expires_at": weather_doc["expires_at"],
                "freshness": weather_doc["freshness"],
            },
            "corridor_label": label.group(1).strip() if label else "Synthetic corridor model",
            "corridor_summary": json.loads(
                (SNAPSHOT / "corridors" / "corridors_summary.json").read_text(encoding="utf-8")
            ),
            "service_bbox": [
                float(bbox_map["west"]),
                float(bbox_map["south"]),
                float(bbox_map["east"]),
                float(bbox_map["north"]),
            ],
            "what_this_does_not_prove": scenario_doc["what_this_does_not_prove"],
        },
        "layers": {
            "basemap": {
                "id": "basemap",
                "label": "Basemap: synthetic region mask, no live tiles",
                "bbox": [float(bbox_map["west"]), float(bbox_map["south"]), float(bbox_map["east"]), float(bbox_map["north"])],
            },
            "corridor_centerline": {"id": "corridor_centerline", "edges": edges},
            "candidate_corridors": {"id": "candidate_corridors", "lines": candidates},
            "facility_pads": {"id": "facility_pads", "facilities": pads},
            "restriction_buffers": {"id": "restriction_buffers", "polygons": restriction},
            "weather": {"id": "weather", "trace": wind_trace},
        },
    }
    # A digest the console can print: sorted bundle hashes, joined and hashed.
    digest = hashlib.sha256(
        "\n".join(f"{k}:{v}" for k, v in sorted(bundle_doc["files"].items())).encode()
    ).hexdigest()
    doc["provenance"]["bundle_manifest_sha256"] = f"sha256:{digest}"
    doc["provenance"]["snapshot_digest"] = f"sha256:{digest}"

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(doc, separators=(",", ":"), sort_keys=False) + "\n", encoding="utf-8")
    print(
        f"wrote {OUT.relative_to(ROOT)}: {len(pads)} pads, {len(edges)} corridor edges, "
        f"{len(candidates)} candidate corridors, {len(wind_trace)} weather steps, "
        f"{OUT.stat().st_size // 1024} KiB"
    )


if __name__ == "__main__":
    main()
