"""Planar geometry and lane-crossing queries for the corridor graph.

Coordinates are integer millimetres on one :class:`~droneops_geom_core.LocalPlane`
anchored inside the bundle, and every predicate here is exact integer arithmetic:
a crossing is decided by orientation signs, not by an epsilon comparison, so the
same bundle gives the same crossing set on every machine and under every hash seed.

The crossing set is a *topological* statement - these two centrelines meet. What
a meeting costs in protected volume is a geom-4d question, asked through the
planner's injected port.
"""

from __future__ import annotations

from dataclasses import dataclass

from droneops_geom_core import (
    CHANDIGARH_UTM_EPSG,
    AltitudeReference,
    Cell3D,
    GeoPoint,
    GridSpec,
    LocalPlane,
    Millimetre,
    PlanarOffset,
    geodesic_distance_mm,
    metres_to_millimetres,
)

from .errors import TopologyError
from .model import CorridorEdge, CorridorNode, GeometryPoint

DEFAULT_GRID: GridSpec = GridSpec(
    epsg=CHANDIGARH_UTM_EPSG,
    cell_size_mm=Millimetre(10_000),
    vertical_band_mm=Millimetre(10_000),
)
"""The pre-filter lattice: 10 m cells, 10 m vertical bands, Chandigarh UTM frame.

Cell size and band height are experiment parameters (conventions section 3), read
from the caller so a policy change cannot hide inside this module.
"""


@dataclass(frozen=True)
class PlanarSegment:
    """One centreline segment in integer millimetres."""

    start: PlanarOffset
    end: PlanarOffset

    def crosses(self, other: PlanarSegment) -> bool:
        """True when the two open segments cross at an interior point.

        Sharing an endpoint is *not* a crossing: adjacent lanes of one corridor
        meet by design, and calling that a crossing would make every lane conflict
        with its own neighbours.
        """
        signs = (
            _orientation(self.start, self.end, other.start),
            _orientation(self.start, self.end, other.end),
            _orientation(other.start, other.end, self.start),
            _orientation(other.start, other.end, self.end),
        )
        if all(sign == 0 for sign in signs):
            return _collinear_overlap(self, other)
        return signs[0] * signs[1] < 0 and signs[2] * signs[3] < 0


def _orientation(a: PlanarOffset, b: PlanarOffset, c: PlanarOffset) -> int:
    """Sign of the 2D cross product of ``ab`` with ``ac``: -1, 0 or 1."""
    value = (b.east_mm - a.east_mm) * (c.north_mm - a.north_mm) - (
        b.north_mm - a.north_mm
    ) * (c.east_mm - a.east_mm)
    return (value > 0) - (value < 0)


def _collinear_overlap(left: PlanarSegment, right: PlanarSegment) -> bool:
    """Interior overlap of two collinear segments, exclusive of shared endpoints."""

    def projection(offset: PlanarOffset, anchor: PlanarOffset, delta: tuple[int, int]) -> int:
        east, north = delta
        if east != 0:
            return offset.east_mm - anchor.east_mm
        return offset.north_mm - anchor.north_mm

    delta = (left.end.east_mm - left.start.east_mm, left.end.north_mm - left.start.north_mm)
    left_a = projection(left.start, left.start, delta)
    left_b = projection(left.end, left.start, delta)
    low, high = sorted((left_a, left_b))
    right_a = projection(right.start, left.start, delta)
    right_b = projection(right.end, left.start, delta)
    r_low, r_high = sorted((right_a, right_b))
    return r_low < high and low < r_high


def plane_for(nodes: tuple[CorridorNode, ...]) -> LocalPlane:
    """A projection anchored at the node nearest the bundle's coordinate centroid.

    Deterministic: the centroid is computed from sorted node ids and ties break on
    node id, so two loads of one bundle anchor the plane at the same millimetre.
    """
    if not nodes:
        raise TopologyError("cannot anchor a plane to an empty node set")
    by_id = sorted(nodes, key=lambda node: node.node_id)
    lat = sum(node.position.lat_deg for node in by_id) / len(by_id)
    lon = sum(node.position.lon_deg for node in by_id) / len(by_id)
    centroid = GeoPoint(lat, lon)
    best = min(
        by_id,
        key=lambda node: (
            geodesic_distance_mm(node.position, centroid),
            node.node_id,
        ),
    )
    return LocalPlane(best.position)


def segment_offsets(
    edge: CorridorEdge, plane: LocalPlane
) -> tuple[PlanarSegment, ...]:
    """Consecutive centreline vertices as planar segments."""
    offsets = tuple(
        plane.offset_of(vertex.point)
        for vertex in sorted_vertices(edge.geometry)
    )
    return tuple(
        PlanarSegment(offsets[index], offsets[index + 1])
        for index in range(len(offsets) - 1)
    )


def sorted_vertices(geometry: tuple[GeometryPoint, ...]) -> tuple[GeometryPoint, ...]:
    """The declared vertex order, which *is* the direction of travel.

    Sorting by coordinate would reverse a lane whose vertices run south to north
    and then hand a crossing predicate the wrong segment.
    """
    return geometry


def swept_cells(
    edge: CorridorEdge,
    plane: LocalPlane,
    grid: GridSpec = DEFAULT_GRID,
    *,
    level: int | None = None,
    reference: AltitudeReference | None = None,
) -> tuple[Cell3D, ...]:
    """Volume cells the declared centreline passes through, ascending by token.

    One cell per vertex plus one per segment midpoint: a pre-filter set, not a
    reservation set. The precise sweep - margins, uncertainty, climb envelope -
    belongs to geom-4d and is obtained by the planner through its injected port,
    so this graph never has to claim it built a protected volume.
    """
    precision = grid.max_level if level is None else level
    altitude_mm = edge.cruise_altitude_mm()
    used_reference = reference or edge.reference
    offsets = [plane.offset_of(vertex.point) for vertex in edge.geometry]
    probes: list[PlanarOffset] = list(offsets)
    for index in range(len(offsets) - 1):
        probes.append(
            PlanarOffset(
                Millimetre((offsets[index].east_mm + offsets[index + 1].east_mm) // 2),
                Millimetre((offsets[index].north_mm + offsets[index + 1].north_mm) // 2),
            )
        )
    cells: dict[str, Cell3D] = {}
    for offset in probes:
        cell = grid.volume_cell_of(
            offset.east_mm,
            offset.north_mm,
            altitude_mm,
            level=precision,
            reference=used_reference,
        )
        cells[cell.token()] = cell
    return tuple(cells[token] for token in sorted(cells))


def lane_width_mm(width_m: int) -> Millimetre:
    """Bundle metres to integer millimetres, quantised once."""
    return metres_to_millimetres(width_m)


__all__ = [
    "DEFAULT_GRID",
    "PlanarSegment",
    "lane_width_mm",
    "plane_for",
    "segment_offsets",
    "swept_cells",
]
