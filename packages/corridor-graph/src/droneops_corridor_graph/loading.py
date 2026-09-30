"""Load the directed lane graph from a frozen scenario bundle.

The topology is *read*, never written here: node and edge identity, band rules,
lane direction, facility capacity and closures all come from
``simulation/scenarios/<bundle>/``. Nothing in this module hard-codes a Chandigarh
lane, and the test suite proves it by re-loading a bundle whose ids and lengths are
rewritten and asserting the graph follows.

Two boundary decisions matter:

* **the YAML decode is injected.** ``corridor-graph`` declares no parser and no
  filesystem dependency: :class:`BundleSource` is implemented by the caller
  (``apps/sim-runner`` already reads bundles this way) and by the tests. The graph
  therefore cannot reach the outside world by itself, and it hashes the *text* the
  source gives it, so a document that no longer matches ``bundle.yaml`` is refused;
* **``geom-4d`` is not imported.** It is being built in parallel and owns swept
  volumes; this package stops at declared centrelines and the volume cells those
  centrelines pass through.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from droneops_geom_core import (
    AltitudeReference,
    GeoPoint,
    Interval,
    checked_add,
    metres_to_millimetres,
    reference_from_token,
    to_microseconds,
)

from .errors import BundleIntegrityError, GraphLoadError, TopologyError
from .geometry import lane_width_mm, plane_for, swept_cells
from .graph import CorridorGraph
from .model import (
    AltitudeBandRule,
    CorridorEdge,
    CorridorNode,
    FacilityAttributes,
    GeometryPoint,
    GraphSourceMetadata,
    LaneDirection,
    LaneRule,
    NodeKind,
    canonical_digest,
    minutes_to_us,
)
from .overlay import overlay_from_document

CORRIDORS_DOCUMENT = "corridors.yaml"
FACILITIES_DOCUMENT = "facilities.yaml"
BUNDLE_DOCUMENT = "bundle.yaml"
SCENARIO_DOCUMENT = "scenario.yaml"

_KIND_BY_TOKEN: Mapping[str, NodeKind] = {
    "hub": NodeKind.HUB,
    "node": NodeKind.NODE,
    "depot": NodeKind.DEPOT,
    "pad": NodeKind.PAD,
}


class BundleSource(Protocol):
    """A caller-supplied reader over one frozen scenario directory."""

    def text(self, name: str) -> str:
        """Raw document bytes as text - hashed to verify manifest integrity."""
        ...

    def document(self, name: str) -> Mapping[str, object]:
        """Parsed document. Implementations may cache; contents must not mutate."""
        ...


@dataclass(frozen=True)
class BundleManifest:
    """The integrity and provenance a bundle states about itself."""

    scenario_id: str
    schema_version: int
    seed: int
    epoch_us: int
    declared_hashes: Mapping[str, str]
    altitude_reference: AltitudeReference
    expires_at_us: int | None
    retrieved_at_us: int | None

    @property
    def bundle_sha256(self) -> str:
        """Digest over the *declared* file hashes, name-sorted.

        Declared rather than recomputed: the run identity has to survive a caller
        that reads the documents twice, and a mismatch is refused at load, so by
        the time this is read the two are already equal.
        """
        return canonical_digest(
            [(name, self.declared_hashes[name]) for name in sorted(self.declared_hashes)]
        )


def load_manifest(source: BundleSource) -> BundleManifest:
    """Parse ``bundle.yaml`` and verify every declared document hash."""
    document = source.document(BUNDLE_DOCUMENT)
    scenario_id = _text(document, "scenario_id")
    declared = document.get("files")
    if not isinstance(declared, Mapping):
        raise GraphLoadError("bundle manifest has no 'files' hash map")
    hashes = {str(name): _text_value(value, f"files.{name}") for name, value in declared.items()}
    for name, digest in sorted(hashes.items()):
        observed = sha256_text(source.text(name))
        if observed != digest:
            raise BundleIntegrityError(
                f"{name} hashes to {observed} but the manifest declares {digest}"
            )
    runtime = document.get("runtime", {})
    policies = source.document("policies.yaml") if "policies.yaml" in hashes else {}
    epoch_value = policies.get("epoch") if isinstance(policies, Mapping) else None
    if not isinstance(epoch_value, str):
        raise GraphLoadError("policies.yaml must state an epoch for bundle minutes")
    altitude_token = str(runtime.get("altitude_reference", "AGL")) if isinstance(runtime, Mapping) else "AGL"
    return BundleManifest(
        scenario_id=scenario_id,
        schema_version=_whole(document, "schema_version"),
        seed=_whole(document, "seed"),
        epoch_us=to_microseconds(_timestamp(epoch_value)),
        declared_hashes=hashes,
        altitude_reference=reference_from_token(altitude_token),
        expires_at_us=_optional_timestamp(_nested(document, "source_manifest", "restrictions", "expires_at")),
        retrieved_at_us=_optional_timestamp(_nested(document, "source_manifest", "weather", "retrieved_at")),
    )


def load_graph(source: BundleSource, *, as_of_us: int | None = None) -> CorridorGraph:
    """Build the corridor graph plus its closure overlay from one bundle."""
    manifest = load_manifest(source)
    if (
        as_of_us is not None
        and manifest.expires_at_us is not None
        and as_of_us > manifest.expires_at_us
    ):
        raise GraphLoadError(
            f"bundle {manifest.scenario_id} restriction data expired at "
            f"{manifest.expires_at_us}; as-of {as_of_us} fails closed"
        )
    corridors = source.document(CORRIDORS_DOCUMENT)
    reference = reference_from_token(_text(corridors, "altitude_reference"))
    bands = _band_rules(corridors, reference)
    nodes = _nodes(source.document(FACILITIES_DOCUMENT), manifest.epoch_us, reference)
    edges = _edges(corridors, bands, reference)
    plane = plane_for(tuple(nodes.values()))
    grid_cells = {edge_id: swept_cells(edge, plane) for edge_id, edge in sorted(edges.items())}
    overlay = overlay_from_document(
        corridors,
        epoch_us=manifest.epoch_us,
        known_edges=tuple(sorted(edges)),
    )
    scenario = source.document(SCENARIO_DOCUMENT) if SCENARIO_DOCUMENT in manifest.declared_hashes else {}
    metadata = GraphSourceMetadata(
        scenario_id=manifest.scenario_id,
        schema_version=manifest.schema_version,
        bundle_sha256=manifest.bundle_sha256,
        graph_version=_graph_version(nodes, edges),
        documents=manifest.declared_hashes,
        altitude_reference=reference,
        source=_text(corridors, "source"),
        epoch_us=manifest.epoch_us,
        authoritative=bool(corridors.get("authoritative", False)),
        safety_claim=bool(corridors.get("safety_claim", False)),
        expires_at_us=manifest.expires_at_us,
        retrieved_at_us=manifest.retrieved_at_us,
    )
    return CorridorGraph(
        metadata=metadata,
        nodes=nodes,
        edges=edges,
        bands=bands,
        overlay=overlay,
        plane=plane,
        edge_cells=grid_cells,
        scenario_title=_text_value(scenario.get("title", ""), "scenario.title")
        if scenario
        else manifest.scenario_id,
    )


def sha256_text(text: str) -> str:
    """Lowercase hex sha256 of a document's text - the bundle manifest's own format."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _band_rules(
    corridors: Mapping[str, object], reference: AltitudeReference
) -> Mapping[str, AltitudeBandRule]:
    raw = corridors.get("bands")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise GraphLoadError("corridors document must declare altitude bands")
    rules: dict[str, AltitudeBandRule] = {}
    for entry in raw:
        if not isinstance(entry, Mapping):
            raise GraphLoadError("a band entry must be a mapping")
        band = _text(entry, "band")
        floor_mm = metres_to_millimetres(_number(entry, "floor_m_agl"))
        ceiling_mm = metres_to_millimetres(_number(entry, "ceiling_m_agl"))
        rule = AltitudeBandRule(
            band=band,
            floor_mm=floor_mm,
            ceiling_mm=ceiling_mm,
            speed_cap_mm_per_s=metres_to_millimetres(_number(entry, "speed_cap_mps")),
            reference=reference,
        )
        if band in rules:
            raise GraphLoadError(f"band {band} declared twice")
        rules[band] = rule
    if not rules:
        raise GraphLoadError("no altitude bands declared")
    return {band: rules[band] for band in sorted(rules)}


def _nodes(
    facilities: Mapping[str, object], epoch_us: int, reference: AltitudeReference
) -> Mapping[str, CorridorNode]:
    raw = facilities.get("facilities")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise GraphLoadError("facilities document must declare facilities")
    nodes: dict[str, CorridorNode] = {}
    for entry in raw:
        if not isinstance(entry, Mapping):
            raise GraphLoadError("a facility entry must be a mapping")
        facility_id = _text(entry, "facility_id")
        kind_token = _text(entry, "kind")
        if kind_token not in _KIND_BY_TOKEN:
            raise TopologyError(
                f"facility {facility_id} declares kind {kind_token!r} which is not "
                "hub, node, depot or pad"
            )
        location = entry.get("location")
        if not isinstance(location, Mapping):
            raise TopologyError(f"facility {facility_id} has no location")
        shifts = tuple(
            Interval(
                minutes_to_us(_pair[0], epoch_us),
                minutes_to_us(_pair[1], epoch_us),
            )
            for _pair in sorted(
                (
                    (_whole_item(crew[0], "crew start"), _whole_item(crew[1], "crew end"))
                    for crew in entry.get("crew_shifts", [])
                    if isinstance(crew, Sequence) and len(crew) == 2
                ),
                key=lambda item: item[0],
            )
        )
        attributes = FacilityAttributes(
            facility_id=facility_id,
            kind=kind_token,
            pads=_whole(entry, "pads"),
            charger_bays=_whole(entry, "charger_bays"),
            open_us=minutes_to_us(_whole(entry, "open_minute"), epoch_us),
            close_us=minutes_to_us(_whole(entry, "close_minute"), epoch_us),
            loading_turnaround_us=checked_add(
                0, _whole(entry, "loading_turnaround_min") * 60 * 1_000_000
            ),
            offload_turnaround_us=checked_add(
                0, _whole(entry, "offload_turnaround_min") * 60 * 1_000_000
            ),
            crew_shifts=shifts,
        )
        point_reference = reference_from_token(_text(location, "altitude_reference"))
        nodes[facility_id] = CorridorNode(
            node_id=facility_id,
            position=GeoPoint(
                _whole(location, "lat_e6") / 1_000_000.0, _whole(location, "lon_e6") / 1_000_000.0
            ),
            ground_altitude_mm=metres_to_millimetres(_number(location, "alt_m_agl")),
            reference=point_reference,
            kind=_KIND_BY_TOKEN[kind_token],
            synthetic=bool(entry.get("synthetic", True)),
            authoritative=bool(facilities.get("authoritative", False)),
            facility=attributes,
        )
    if not nodes:
        raise GraphLoadError("facilities document declares no nodes")
    return {node_id: nodes[node_id] for node_id in sorted(nodes)}


def _edges(
    corridors: Mapping[str, object],
    bands: Mapping[str, AltitudeBandRule],
    reference: AltitudeReference,
) -> Mapping[str, CorridorEdge]:
    raw = corridors.get("edges")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise GraphLoadError("corridors document must declare edges")
    declared: list[CorridorEdge] = []
    for entry in raw:
        if not isinstance(entry, Mapping):
            raise GraphLoadError("a lane entry must be a mapping")
        corridor_id = _text(entry, "corridor_id")
        band_token = _text(entry, "band")
        if band_token not in bands:
            raise TopologyError(
                f"lane {corridor_id} declares band {band_token!r} which no band rule defines"
            )
        rule_band = bands[band_token]
        geometry = _geometry(entry, reference)
        width_mm = lane_width_mm(_whole(entry, "width_m"))
        declared.append(
            CorridorEdge(
                edge_id=corridor_id,
                from_node=_text(entry, "from"),
                to_node=_text(entry, "to"),
                lane_group_id=corridor_id,
                lane_direction=LaneDirection.ONE_WAY,
                band=band_token,
                length_mm=metres_to_millimetres(_number(entry, "length_m")),
                rule=LaneRule(
                    speed_cap_mm_per_s=rule_band.speed_cap_mm_per_s,
                    max_wind_mm_per_s=metres_to_millimetres(_number(entry, "max_wind_ms")),
                    width_mm=width_mm,
                    band=band_token,
                ),
                geometry=geometry,
                reference=reference,
                synthetic=bool(entry.get("synthetic", True)),
            )
        )
    by_id = {edge.edge_id: edge for edge in declared}
    if len(by_id) != len(declared):
        raise GraphLoadError("duplicate corridor_id in the corridors document")
    paired = _assign_lane_groups(declared, by_id)
    return {edge_id: paired[edge_id] for edge_id in sorted(paired)}


def _assign_lane_groups(
    declared: Sequence[CorridorEdge], by_id: Mapping[str, CorridorEdge]
) -> Mapping[str, CorridorEdge]:
    """Pair reverse lanes that describe one centreline flown both ways.

    The bundles carry no direction field, so the rule is stated here once: two
    reverse edges are one bidirectional lane when they declare identical length,
    band, width and wind cap. ``chandigarh-basic`` satisfies that on all 22 lanes;
    ``chandigarh-synthetic-v1`` never does - its reverse pairs differ by 400 m, and
    some change band - so all 26 stay one-way.
    """
    result: dict[str, CorridorEdge] = dict(by_id)
    for edge in sorted(declared, key=CorridorEdge.sort_key):
        reverse = _reverse_of(edge, declared)
        if reverse is None or edge.edge_id >= reverse.edge_id:
            continue
        group_id = f"{edge.edge_id}+{reverse.edge_id}"
        bidirectional = _same_centreline(edge, reverse)
        for member in (edge, reverse):
            result[member.edge_id] = _with_group(
                member,
                group_id,
                LaneDirection.BIDIRECTIONAL if bidirectional else LaneDirection.ONE_WAY,
            )
    return result


def _reverse_of(edge: CorridorEdge, declared: Sequence[CorridorEdge]) -> CorridorEdge | None:
    matches = [item for item in declared if item.from_node == edge.to_node and item.to_node == edge.from_node]
    return min(matches, key=CorridorEdge.sort_key) if matches else None


def _same_centreline(left: CorridorEdge, right: CorridorEdge) -> bool:
    return (
        left.length_mm == right.length_mm
        and left.band == right.band
        and left.rule.width_mm == right.rule.width_mm
        and left.rule.max_wind_mm_per_s == right.rule.max_wind_mm_per_s
    )


def _with_group(edge: CorridorEdge, group_id: str, direction: LaneDirection) -> CorridorEdge:
    return CorridorEdge(
        edge_id=edge.edge_id,
        from_node=edge.from_node,
        to_node=edge.to_node,
        lane_group_id=group_id,
        lane_direction=direction,
        band=edge.band,
        length_mm=edge.length_mm,
        rule=edge.rule,
        geometry=edge.geometry,
        reference=edge.reference,
        synthetic=edge.synthetic,
        authoritative=edge.authoritative,
    )


def _geometry(entry: Mapping[str, object], reference: AltitudeReference) -> tuple[GeometryPoint, ...]:
    raw = entry.get("geometry")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)) or len(raw) < 2:
        raise TopologyError(f"lane {_text(entry, 'corridor_id')} needs at least two vertices")
    points: list[GeometryPoint] = []
    for vertex in raw:
        if not isinstance(vertex, Mapping):
            raise TopologyError("a centreline vertex must be a mapping")
        points.append(
            GeometryPoint(
                point=GeoPoint(
                    _whole(vertex, "lat_e6") / 1_000_000.0,
                    _whole(vertex, "lon_e6") / 1_000_000.0,
                ),
                altitude_mm=metres_to_millimetres(_number(vertex, "alt_m_agl")),
                reference=reference_from_token(_text(vertex, "altitude_reference")),
            )
        )
    return tuple(points)


def _graph_version(
    nodes: Mapping[str, CorridorNode], edges: Mapping[str, CorridorEdge]
) -> str:
    payload = [
        sorted(nodes),
        [
            (
                edge.edge_id,
                edge.from_node,
                edge.to_node,
                edge.lane_group_id,
                edge.lane_direction.value,
                edge.band,
                edge.length_mm,
                edge.rule.width_mm,
                edge.rule.max_wind_mm_per_s,
            )
            for edge in sorted(edges.values(), key=CorridorEdge.sort_key)
        ],
    ]
    return f"corridor-graph-v1:{canonical_digest(payload)[7:23]}"


def _text(document: Mapping[str, object], key: str) -> str:
    return _text_value(document.get(key), key)


def _text_value(value: object, key: str) -> str:
    if not isinstance(value, str) or not value:
        raise GraphLoadError(f"field {key!r} must be a non-empty string")
    return value


def _whole(document: Mapping[str, object], key: str) -> int:
    value = document.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise GraphLoadError(f"field {key!r} must be an integer, got {value!r}")
    return value


def _whole_item(value: object, key: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise GraphLoadError(f"field {key!r} must be an integer, got {value!r}")
    return value


def _number(document: Mapping[str, object], key: str) -> float:
    """A declared metre value: ints stay ints, floats are quantised once."""
    value = document.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GraphLoadError(f"field {key!r} must be a number, got {value!r}")
    return float(value)


def _nested(document: Mapping[str, object], *path: str) -> object:
    current: object = document
    for token in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(token)
    return current


def _timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _optional_timestamp(value: object) -> int | None:
    if not isinstance(value, str):
        return None
    return to_microseconds(_timestamp(value))


__all__ = [
    "BundleManifest",
    "BundleSource",
    "load_graph",
    "load_manifest",
    "sha256_text",
]
