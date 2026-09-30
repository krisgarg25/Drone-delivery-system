"""The lane-graph value model: nodes, directed edges, bands, merges, provenance.

Everything here is a frozen value object built from a frozen scenario bundle. Two
invariants shape the whole module (``docs/conventions.md`` sections 3 and 4):

* **integers only** - distance is millimetres, speed is millimetres per second,
  time is microseconds. A bundle value that arrives as a float is quantised once,
  at load, and the quantisation is recorded on the object that carries it;
* **provenance that cannot be stripped** - every node, edge and band carries
  ``authoritative=False`` and, where the bundle says so, ``synthetic=True``.
  A lane is never described as safe, only as *declared*.

Sequence-returning members document their total order. Nothing in this module
iterates an unsorted dict or set to produce output.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from collections.abc import Mapping, Sequence

from droneops_geom_core import (
    MICROS_PER_MINUTE,
    AltitudeBand,
    AltitudeReference,
    GeoPoint,
    Interval,
    as_millimetres,
    checked_add,
    traversal_microseconds,
)

from .errors import AltitudeBandError, TopologyError

MICROS_PER_SECOND: int = 1_000_000
"""Kernel microseconds per second; used where a bundle speaks in seconds."""


class NodeKind(StrEnum):
    """What a lane endpoint is, taken from the bundle's facility kind."""

    HUB = "hub"
    NODE = "node"
    DEPOT = "depot"
    PAD = "pad"
    VIRTUAL = "virtual"


class LaneDirection(StrEnum):
    """Whether a centreline serves both directions or exactly one.

    Derived from the bundle, never asserted: ``BIDIRECTIONAL`` requires a reverse
    lane that declares the same length, band, width and wind cap, which is how the
    bundles encode one centreline flown both ways. Anything else - including the
    ``chandigarh-synthetic-v1`` lanes, whose reverse pairs differ by 400 m and
    sometimes by band - is two independent one-way lanes.
    """

    ONE_WAY = "one_way"
    BIDIRECTIONAL = "bidirectional"


class MergeKind(StrEnum):
    """Traffic pattern at a node, computed from in- and out-degree."""

    THROUGH = "through"
    CONVERGENCE = "convergence"
    DIVERGENCE = "divergence"
    INTERSECTION = "intersection"


@dataclass(frozen=True)
class GeometryPoint:
    """One declared centreline vertex with its explicit altitude reference."""

    point: GeoPoint
    altitude_mm: int
    reference: AltitudeReference

    def __post_init__(self) -> None:
        if self.reference is AltitudeReference.UNSPECIFIED:
            raise AltitudeBandError("a centreline vertex must state its altitude reference (I9)")
        if self.altitude_mm < 0:
            raise AltitudeBandError(f"altitude {self.altitude_mm} mm is below the datum")

    def sort_key(self) -> tuple[float, float, int]:
        """Ascending ``(lat, lon, altitude_mm)`` - enumeration only."""
        return (self.point.lat_deg, self.point.lon_deg, self.altitude_mm)


@dataclass(frozen=True)
class AltitudeBandRule:
    """A declared band: floor, ceiling, speed cap, all integers.

    ``floor_mm`` is inclusive and ``ceiling_mm`` exclusive, so a lane at exactly
    50 m AGL belongs to band B and not to band A. Two adjacent bands that both
    claimed a boundary altitude would give two answers to "which lane may this
    aircraft use".
    """

    band: str
    floor_mm: int
    ceiling_mm: int
    speed_cap_mm_per_s: int
    reference: AltitudeReference

    def __post_init__(self) -> None:
        if not self.band:
            raise AltitudeBandError("band name must not be empty")
        if self.ceiling_mm <= self.floor_mm:
            raise AltitudeBandError(
                f"band {self.band} ceiling {self.ceiling_mm} must exceed floor "
                f"{self.floor_mm}"
            )
        if self.speed_cap_mm_per_s <= 0:
            raise AltitudeBandError(f"band {self.band} speed cap must be positive")

    def contains_altitude_mm(self, altitude_mm: int) -> bool:
        return self.floor_mm <= altitude_mm < self.ceiling_mm

    def to_band(self) -> AltitudeBand:
        """The geom-core band for this rule, reference attached.

        The band is built from millimetres so the declared floor and ceiling are
        not re-rounded through a metre float.
        """
        return AltitudeBand(
            self.reference, as_millimetres(self.floor_mm), as_millimetres(self.ceiling_mm)
        )

    def sort_key(self) -> tuple[str, int, int]:
        return (self.band, self.floor_mm, self.ceiling_mm)


@dataclass(frozen=True)
class LaneRule:
    """The rules that bind a lane: band cap, wind cap, width and merge class.

    Separated from :class:`CorridorEdge` because an overlay restriction *replaces*
    a rule for a window without rewriting the lane itself.
    """

    speed_cap_mm_per_s: int
    max_wind_mm_per_s: int
    width_mm: int
    band: str
    requires_survey: bool = True

    def __post_init__(self) -> None:
        if self.speed_cap_mm_per_s <= 0 or self.width_mm <= 0:
            raise TopologyError("lane rule needs a positive speed cap and width")
        if self.max_wind_mm_per_s <= 0:
            raise TopologyError("lane rule needs a positive wind cap")

    def is_wind_allowed_mm_per_s(self, wind_mm_per_s: int) -> bool:
        return wind_mm_per_s <= self.max_wind_mm_per_s


@dataclass(frozen=True)
class FacilityAttributes:
    """Pad, charger and crew capacity declared for a lane endpoint.

    Owned by the ``facilities`` package in the target architecture; that package is
    a Phase 0 stub, so the graph carries only what the bundle states and the
    planner asks an injected facility port for windows. Windows are absolute
    kernel microseconds, computed from the bundle epoch once, at load.
    """

    facility_id: str
    kind: str
    pads: int
    charger_bays: int
    open_us: int
    close_us: int
    loading_turnaround_us: int
    offload_turnaround_us: int
    crew_shifts: tuple[Interval, ...] = ()

    def __post_init__(self) -> None:
        if self.close_us <= self.open_us:
            raise TopologyError(f"facility {self.facility_id} closes before it opens")
        if self.pads < 0 or self.charger_bays < 0:
            raise TopologyError(f"facility {self.facility_id} declares negative capacity")

    @property
    def operating_window(self) -> Interval:
        return Interval.of(self.open_us, self.close_us)

    def crew_windows(self) -> tuple[Interval, ...]:
        """Crew shifts ascending; an empty tuple means no crew constraint stated."""
        return tuple(sorted(self.crew_shifts, key=lambda item: (item.start, item.end)))


@dataclass(frozen=True)
class CorridorNode:
    """A lane endpoint with its position, declared ground altitude and capacity."""

    node_id: str
    position: GeoPoint
    ground_altitude_mm: int
    reference: AltitudeReference
    kind: NodeKind
    synthetic: bool = True
    authoritative: bool = False
    facility: FacilityAttributes | None = None

    def __post_init__(self) -> None:
        if not self.node_id:
            raise TopologyError("node id must not be empty")

    def merge_degree(self) -> tuple[int, int]:
        """``(in_degree, out_degree)`` is filled in by the graph, not here."""
        return (0, 0)


@dataclass(frozen=True)
class CorridorEdge:
    """One directed lane traversal between two nodes.

    ``lane_group_id`` names the physical centreline: a bidirectional pair shares
    one group, a one-way lane is its own group. Closures on either direction of a
    group therefore have to be stated per direction in the bundle, which is exactly
    how ``chandigarh-synthetic-v1`` states them.
    """

    edge_id: str
    from_node: str
    to_node: str
    lane_group_id: str
    lane_direction: LaneDirection
    band: str
    length_mm: int
    rule: LaneRule
    geometry: tuple[GeometryPoint, ...]
    reference: AltitudeReference
    synthetic: bool = True
    authoritative: bool = False

    def __post_init__(self) -> None:
        if self.length_mm <= 0:
            raise TopologyError(f"lane {self.edge_id} has non-positive length")
        if self.from_node == self.to_node:
            raise TopologyError(f"lane {self.edge_id} is a self-loop")
        if len(self.geometry) < 2:
            raise TopologyError(f"lane {self.edge_id} needs at least two centreline vertices")

    def cruise_time_us(self) -> int:
        """Traversal time at the lane's own speed cap, ceilinged.

        Rounded up on purpose: a traversal estimate rounded down is a reservation
        that releases while an aircraft is still inside the volume.
        """
        return traversal_microseconds(
            self.length_mm / 1000.0, self.rule.speed_cap_mm_per_s
        )

    def altitude_profile(self) -> tuple[GeometryPoint, ...]:
        """Declared vertices ascending along the direction of travel."""
        return self.geometry

    def cruise_altitude_mm(self) -> int:
        """The declared vertex altitude; a lane has one cruise altitude in these bundles."""
        altitudes = {vertex.altitude_mm for vertex in self.geometry}
        if len(altitudes) != 1:
            raise TopologyError(
                f"lane {self.edge_id} declares altitudes {sorted(altitudes)}; the Phase 2 "
                "model reserves one band per lane and a varying profile is a geom-4d question"
            )
        return next(iter(altitudes))

    def sort_key(self) -> tuple[str, str, str]:
        return (self.from_node, self.to_node, self.edge_id)


@dataclass(frozen=True)
class MergeNode:
    """A node where traffic joins, splits or crosses, with the conflicting pairs.

    ``conflict_pairs`` is the set of ``(inbound, outbound)`` lane pairs that share
    this node and are not the same lane group: those are the merge volumes a
    reservation has to protect, derived from topology rather than hard-coded.
    """

    node_id: str
    kind: MergeKind
    inbound: tuple[str, ...]
    outbound: tuple[str, ...]
    conflict_pairs: tuple[tuple[str, str], ...]

    def sort_key(self) -> tuple[str, str]:
        return (self.node_id, self.kind.value)


@dataclass(frozen=True)
class GraphSourceMetadata:
    """Where the graph came from and which bytes produced it.

    ``graph_version`` is a content hash of the topology, so two loads of the same
    bundle agree without anyone editing a version string, and a bundle edit changes
    the version on its own.
    """

    scenario_id: str
    schema_version: int
    bundle_sha256: str
    graph_version: str
    documents: Mapping[str, str]
    altitude_reference: AltitudeReference
    source: str
    epoch_us: int
    authoritative: bool = False
    safety_claim: bool = False
    requires_survey: bool = True
    expires_at_us: int | None = None
    retrieved_at_us: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "documents", MappingProxyType(dict(self.documents)))
        if not self.graph_version:
            raise TopologyError("graph version must not be empty")

    def document_digest(self) -> str:
        """sha256 over ``name:hash`` pairs in name order - the frozen input identity."""
        return canonical_digest(
            [(name, self.documents[name]) for name in sorted(self.documents)]
        )


def canonical_digest(value: object) -> str:
    """Deterministic ``sha256:`` digest of nested frozen/plain data.

    Sets and mappings are sorted first, so the digest depends on content only,
    never on iteration order (conventions section 4).
    """
    payload = json.dumps(_encode(value), sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


def _encode(value: object) -> object:
    """Plain-Data recursive encoder for :func:`canonical_digest`."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _encode(getattr(value, field.name))
            for field in sorted(
                dataclasses.fields(value), key=lambda item: item.name
            )
        }
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _encode(item) for key, item in sorted(value.items(), key=pair_key)}
    if isinstance(value, (list, tuple, Sequence)) and not isinstance(value, (str, bytes)):
        return [_encode(item) for item in value]
    if isinstance(value, set | frozenset):
        return [_encode(item) for item in sorted(value, key=repr)]
    return value


def pair_key(item: tuple[object, object]) -> str:
    """Sort key for mapping entries of arbitrary key type."""
    return repr(item[0])


def minutes_to_us(minutes: int, epoch_us: int) -> int:
    """Bundle minutes to absolute kernel microseconds.

    Raises:
        TopologyError: the sum leaves the int64 microsecond domain.
    """
    return checked_add(epoch_us, minutes * MICROS_PER_MINUTE)


__all__ = [
    "MICROS_PER_MINUTE",
    "MICROS_PER_SECOND",
    "AltitudeBand",
    "AltitudeBandRule",
    "CorridorEdge",
    "CorridorNode",
    "FacilityAttributes",
    "GeometryPoint",
    "GraphSourceMetadata",
    "Interval",
    "LaneDirection",
    "LaneRule",
    "MergeKind",
    "MergeNode",
    "NodeKind",
    "canonical_digest",
    "minutes_to_us",
]
