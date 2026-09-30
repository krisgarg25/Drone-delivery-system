"""Cheap indexing: which swept volumes are even worth an intersection test.

A full prism-pair sweep is exact and deliberately not cheap. Before running it,
the volumes are projected onto the ``geom_core`` lattice: the level-``level``
volume cells their footprint rasterises onto, the vertical bands their altitude
extent covers, and their absolute time window. Two volumes that share no cell and
no instant cannot touch, so the pair list shrinks from ``n * m`` to the contested
subset.

The pre-filter is a **superset** by construction, and that is the property the
tests hold it to:

* the rasterisation uses true box-versus-footprint intersection, so a footprint
  cannot slip between sampled cell centres;
* the vertical range is the prism's conservative band, not its centre altitude;
* the time window includes the prism's uncertainty padding.

So an exact conflict can never be filtered away. What can happen - and what this
module exists to make cheap rather than to decide - is a pair that survives the
filter and is then cleared by the sweep.

A cell budget is a required argument, not a default: a huge footprint in a fine
grid is a denial-of-service against the evaluator, and the caller owns how many
cells it is willing to touch per volume.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from droneops_geom_core import Cell3D, GridSpec, Interval, Microsecond, floor_div

from . import shapes
from .errors import SweepGeometryError
from .intersect import IntersectionResult, ProtectedPrism, pair_verdict

__all__ = [
    "IndexedVolume",
    "VolumeIndex",
    "index_volumes",
    "prefiltered_pair_verdicts",
    "volume_cells",
]


@dataclass(frozen=True)
class IndexedVolume:
    """One swept volume reduced to the things an index can compare."""

    volume_id: str
    window: Interval
    cells: tuple[Cell3D, ...]

    @property
    def cell_keys(self) -> tuple[str, ...]:
        """Canonical cell keys, already in the volume's own sorted order."""
        return tuple(cell.key() for cell in self.cells)


@dataclass(frozen=True)
class VolumeIndex:
    """A deterministic cell/time bucket set over one sweep.

    ``buckets`` is sorted by cell key and the identifiers inside each bucket are
    sorted, so two runs that built the same index from lists in different orders
    still produce byte-identical output (conventions section 4).
    """

    grid_key: str
    level: int
    volumes: tuple[IndexedVolume, ...]
    buckets: tuple[tuple[str, tuple[str, ...]], ...]

    def window_of(self, volume_id: str) -> Interval:
        for volume in self.volumes:
            if volume.volume_id == volume_id:
                return volume.window
        raise SweepGeometryError(f"{volume_id} is not in this index")

    def candidates(self, other: VolumeIndex) -> tuple[tuple[str, str], ...]:
        """Identifier pairs sharing a cell *and* an instant, in canonical order."""
        right_by_cell = dict(other.buckets)
        right_windows = {volume.volume_id: volume.window for volume in other.volumes}
        pairs: dict[tuple[str, str], None] = {}
        for cell_key, left_ids in self.buckets:
            right_ids = right_by_cell.get(cell_key)
            if right_ids is None:
                continue
            for left_id in left_ids:
                left_window = self.window_of(left_id)
                for right_id in right_ids:
                    if not left_window.overlaps(right_windows[right_id]):
                        continue
                    pairs[(left_id, right_id)] = None
        return tuple(sorted(pairs))


def volume_cells(
    prism: ProtectedPrism,
    grid: GridSpec,
    *,
    level: int,
    max_cells: int,
) -> tuple[Cell3D, ...]:
    """Cells the volume's footprint and altitude band rasterise onto, sorted.

    Raises:
        SweepGeometryError: the footprint is not usable geometry, or the
            rasterisation would exceed ``max_cells``.
    """
    if max_cells <= 0:
        raise SweepGeometryError(f"max_cells must be positive, got {max_cells}")
    footprint = prism.footprint_wkt()
    if not shapes.is_usable(footprint):
        raise SweepGeometryError(f"{prism.volume_id} has no usable footprint for indexing")
    min_x, min_y, max_x, max_y = shapes.bounding_box(footprint)
    cell_size = grid.cell_size_mm
    band = prism.band()
    band_low = floor_div(band.lower_mm, grid.vertical_band_mm)
    band_high = floor_div(band.upper_mm - 1, grid.vertical_band_mm)
    found: dict[tuple[int, int, int, int], Cell3D] = {}
    for bx in range(floor_div(min_x, cell_size), floor_div(max_x, cell_size) + 1):
        for by in range(floor_div(min_y, cell_size), floor_div(max_y, cell_size) + 1):
            if not shapes.box_overlap(
                footprint,
                bx * cell_size,
                by * cell_size,
                (bx + 1) * cell_size,
                (by + 1) * cell_size,
            ):
                continue
            for band_index in range(band_low, band_high + 1):
                cell = Cell3D.covering(
                    grid, bx, by, band_index, level=level, reference=prism.reference
                )
                found[cell.sort_key()] = cell
                if len(found) > max_cells:
                    raise SweepGeometryError(
                        f"{prism.volume_id} rasterises onto more than {max_cells} cells "
                        f"at level {level} of a {cell_size} mm grid; index it at a coarser "
                        "level or raise the budget"
                    )
    return tuple(found[key] for key in sorted(found))


def index_volumes(
    prisms: Sequence[ProtectedPrism],
    grid: GridSpec,
    *,
    level: int,
    max_cells: int,
) -> VolumeIndex:
    """Build the deterministic pre-filter for one swept-volume set."""
    indexed: list[IndexedVolume] = []
    for prism in prisms:
        indexed.append(
            IndexedVolume(
                volume_id=prism.volume_id,
                window=prism.window(),
                cells=volume_cells(prism, grid, level=level, max_cells=max_cells),
            )
        )
    ordered = tuple(sorted(indexed, key=lambda item: item.volume_id))
    buckets: dict[str, set[str]] = {}
    for volume in ordered:
        for cell_key in volume.cell_keys:
            buckets.setdefault(cell_key, set()).add(volume.volume_id)
    return VolumeIndex(
        grid_key=grid.key(),
        level=level,
        volumes=ordered,
        buckets=tuple((cell_key, tuple(sorted(buckets[cell_key]))) for cell_key in sorted(buckets)),
    )


def prefiltered_pair_verdicts(
    left: Sequence[ProtectedPrism],
    right: Sequence[ProtectedPrism],
    *,
    grid: GridSpec,
    level: int,
    max_cells: int,
) -> tuple[tuple[str, str, IntersectionResult], ...]:
    """Exact verdicts for the pairs the pre-filter could not rule out.

    The result is *the same set of conflicts* as testing every pair, with fewer
    tests - which is the only way an index is allowed to be an optimisation.
    """
    left_by_id = {prism.volume_id: prism for prism in left}
    right_by_id = {prism.volume_id: prism for prism in right}
    left_index = index_volumes(left, grid, level=level, max_cells=max_cells)
    right_index = index_volumes(right, grid, level=level, max_cells=max_cells)
    results: list[tuple[str, str, IntersectionResult]] = []
    for left_id, right_id in left_index.candidates(right_index):
        results.append(
            (
                left_id,
                right_id,
                pair_verdict(left_by_id[left_id], right_by_id[right_id]),
            )
        )
    return tuple(results)


def indexed_window_count(index: VolumeIndex) -> Microsecond:
    """Total number of cell-window entries, for a metrics line and nothing else."""
    return Microsecond(sum(len(volume.cells) for volume in index.volumes))
