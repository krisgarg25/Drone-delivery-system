"""Unit tests for grid cells, the geohash3d volume key and neighbourhoods."""

from __future__ import annotations

import pytest

from droneops_geom_core import (
    AltitudeBand,
    AltitudeReference,
    Cell2D,
    Cell3D,
    CellDomainError,
    GridSpec,
    Interval,
    Millimetre,
    ResourceKey,
    cell_sequence_is_disjoint,
    merge_cells_by_token,
    resource_key,
)

SPEC = GridSpec(32643, Millimetre(16_000), Millimetre(10_000))


class TestGridSpec:
    def test_default_bit_budget_yields_six_horizontal_characters(self) -> None:
        assert SPEC.max_level == 6
        assert SPEC.vertical_width == 2
        assert SPEC.index_limit == 16_384

    def test_non_positive_sizes_are_rejected(self) -> None:
        with pytest.raises(CellDomainError):
            GridSpec(32643, Millimetre(0), Millimetre(10_000))
        with pytest.raises(CellDomainError):
            GridSpec(32643, Millimetre(16_000), Millimetre(-1))
        with pytest.raises(CellDomainError):
            GridSpec(0, Millimetre(16_000), Millimetre(10_000))

    def test_a_bit_count_that_is_not_a_multiple_of_five_is_rejected(self) -> None:
        """Truncation must land on a character boundary or the hierarchy is wrong."""
        with pytest.raises(CellDomainError, match="multiple of 5"):
            GridSpec(32643, Millimetre(16_000), Millimetre(10_000), horizontal_bits=14)
        with pytest.raises(CellDomainError, match="multiple of 5"):
            GridSpec(32643, Millimetre(16_000), Millimetre(10_000), vertical_bits=9)

    def test_specification_key_round_trips(self) -> None:
        assert GridSpec.from_key(SPEC.key()) == SPEC
        odd = GridSpec(32644, Millimetre(25_000), Millimetre(5_000), horizontal_bits=20, vertical_bits=15)
        assert GridSpec.from_key(odd.key()) == odd

    def test_a_cell_from_another_crs_is_not_the_same_cell(self) -> None:
        other = GridSpec(32644, Millimetre(16_000), Millimetre(10_000))
        assert SPEC.cell_of(0, 0) != other.cell_of(0, 0)
        assert SPEC.cell_of(0, 0).key() != other.cell_of(0, 0).key()


class TestCellIndexing:
    def test_floor_division_not_truncation(self) -> None:
        """One millimetre west of the origin is not in the origin's cell."""
        assert SPEC.cell_of(-1, -1) == Cell2D(SPEC, -1, -1)
        assert SPEC.cell_of(0, 0) == Cell2D(SPEC, 0, 0)
        assert SPEC.cell_of(15_999, 16_000) == Cell2D(SPEC, 0, 1)

    def test_negative_offsets_stay_the_right_width_apart(self) -> None:
        assert SPEC.cell_of(-16_000, 0) == Cell2D(SPEC, -1, 0)
        assert SPEC.cell_of(-16_001, 0) == Cell2D(SPEC, -2, 0)

    def test_cell_origin_offset_is_the_inverse_quantisation(self) -> None:
        cell = SPEC.cell_of(-40_000, 32_000)
        assert cell.offset_of_origin() == (Millimetre(-48_000), Millimetre(32_000))

    def test_two_d_key_round_trips_standalone(self) -> None:
        cell = SPEC.cell_of(-123_456_789, 9_876_543)
        assert Cell2D.from_key(cell.key()) == cell
        assert Cell2D.from_key(cell.key()).key() == cell.key()

    def test_a_malformed_cell_key_is_rejected(self) -> None:
        with pytest.raises(CellDomainError):
            Cell2D.from_key("2d|32643|16000|10000|15|10|1")
        with pytest.raises(CellDomainError):
            Cell2D.from_key("3d|32643|16000|10000|15|10|1|2")
        with pytest.raises(CellDomainError):
            Cell2D.from_key("2d|32643|16000|10000|15|10|x|y")

    def test_index_outside_the_bit_domain_is_rejected(self) -> None:
        with pytest.raises(CellDomainError, match="domain"):
            Cell2D(SPEC, 16_384, 0)
        with pytest.raises(CellDomainError, match="domain"):
            Cell2D(SPEC, 0, -16_385)

    def test_an_offset_beyond_the_declared_coverage_is_rejected_not_wrapped(self) -> None:
        """16384 cells x 16 m is about 262 km either side of the grid origin."""
        with pytest.raises(CellDomainError, match="domain"):
            SPEC.cell_of(0, 16_385 * 16_000)


class TestNeighbourhoods:
    def test_neighbours_returns_nine_including_self(self) -> None:
        cell = SPEC.cell_of(0, 0)
        found = cell.neighbours()
        assert len(found) == 9
        assert cell in found
        assert {(n.x, n.y) for n in found} == {
            (x, y) for x in (-1, 0, 1) for y in (-1, 0, 1)
        }

    def test_neighbourhood_is_sorted_by_y_then_x(self) -> None:
        found = SPEC.cell_of(16_000, -16_000).neighbourhood(radius=1)
        assert found == tuple(sorted(found, key=Cell2D.sort_key))

    def test_larger_radius_is_the_conservative_prefilter(self) -> None:
        assert len(SPEC.cell_of(0, 0).neighbourhood(radius=2)) == 25
        assert len(SPEC.cell_of(0, 0).neighbourhood(radius=0)) == 1

    def test_membership_is_symmetric(self) -> None:
        centre = SPEC.cell_of(0, 0)
        for cell in centre.neighbourhood(radius=1):
            assert centre in cell.neighbourhood(radius=1)

    def test_negative_radius_is_rejected(self) -> None:
        with pytest.raises(CellDomainError):
            SPEC.cell_of(0, 0).neighbourhood(radius=-1)


class TestGeohash3D:
    @pytest.mark.parametrize("level", [1, 2, 3, 4, 5, 6])
    def test_token_round_trips_at_every_level(self, level: int) -> None:
        for east, north, altitude in [
            (123_456, -98_765, 45_000),
            (0, 0, 0),
            (-1, -1, -19_999),
            (-500_000, 700_000, 120_000),
        ]:
            cell = SPEC.volume_cell_of(
                east, north, altitude, level=level, reference=AltitudeReference.AGL
            )
            token = cell.token()
            assert Cell3D.parse_token(SPEC, token, reference=AltitudeReference.AGL) == cell
            assert Cell3D.parse_token(SPEC, cell.token(), reference=AltitudeReference.AGL).token() == token

    def test_verbose_key_round_trips_with_the_reference(self) -> None:
        cell = SPEC.volume_cell_of(
            123_456, -98_765, 45_000, level=4, reference=AltitudeReference.PAD
        )
        assert Cell3D.from_key(cell.key()) == cell
        assert Cell3D.from_key(cell.key()).reference is AltitudeReference.PAD

    def test_a_key_that_disagrees_with_its_own_token_is_rejected(self) -> None:
        cell = SPEC.volume_cell_of(0, 0, 0, level=6, reference=AltitudeReference.AGL)
        forged = cell.key().replace("|6|", "|5|")
        with pytest.raises(CellDomainError, match="level"):
            Cell3D.from_key(forged)

    def test_truncation_gives_the_containing_block(self) -> None:
        fine = SPEC.volume_cell_of(123_456, -98_765, 0, level=6, reference=AltitudeReference.AGL)
        coarse = fine.parent()
        assert fine.token().split(".")[0].startswith(coarse.token().split(".")[0])
        assert coarse.covers_fine_cell(fine.x, fine.y)
        assert coarse.level == fine.level - 1

    def test_the_whole_parent_chain_contains_the_child(self) -> None:
        fine = SPEC.volume_cell_of(987_654, -123_456, 10_000, level=6, reference=AltitudeReference.AMSL)
        chain = [fine]
        while chain[-1].level > 1:
            chain.append(chain[-1].parent())
        for parent in chain:
            assert parent.covers_fine_cell(fine.x, fine.y)
        with pytest.raises(CellDomainError, match="no parent"):
            chain[-1].parent()

    def test_block_extents_alternate_between_axes(self) -> None:
        """Dropping an odd number of interleaved bits gives x and y different widths."""
        assert SPEC.block_extent(6) == (1, 1)
        assert SPEC.block_extent(5) == (4, 8)
        assert SPEC.block_extent(4) == (32, 32)
        assert SPEC.block_extent(3) == (128, 256)
        assert SPEC.block_extent(1) == (4096, 8192)

    def test_unaligned_cell_is_rejected(self) -> None:
        with pytest.raises(CellDomainError, match="not aligned"):
            Cell3D(SPEC, 5, 1, 0, 0, AltitudeReference.AGL)

    def test_covering_aligns_down_instead_of_raising(self) -> None:
        coarse = Cell3D.covering(SPEC, 7, -7, 3, level=5, reference=AltitudeReference.AGL)
        assert coarse.x == 4 and coarse.y == -8

    def test_band_index_uses_the_fine_lattice_at_every_level(self) -> None:
        low = SPEC.volume_cell_of(0, 0, 5_000, level=6, reference=AltitudeReference.AGL)
        high = SPEC.volume_cell_of(0, 0, 15_000, level=6, reference=AltitudeReference.AGL)
        assert (low.band_index, high.band_index) == (0, 1)

    def test_negative_altitudes_get_negative_band_indices(self) -> None:
        below = SPEC.volume_cell_of(0, 0, -5_000, level=1, reference=AltitudeReference.AMSL)
        assert below.band_index == -1

    def test_band_index_outside_the_vertical_domain_is_rejected(self) -> None:
        with pytest.raises(CellDomainError, match="vertical"):
            Cell3D(SPEC, 6, 0, 0, 512, AltitudeReference.AGL)

    def test_unspecified_reference_is_rejected_in_a_volume_cell(self) -> None:
        with pytest.raises(CellDomainError, match="UNSPECIFIED"):
            Cell3D(SPEC, 6, 0, 0, 0, AltitudeReference.UNSPECIFIED)

    def test_a_token_without_a_vertical_part_is_rejected(self) -> None:
        with pytest.raises(CellDomainError, match="horizontal>.<vertical"):
            Cell3D.parse_token(SPEC, "abcdef", reference=AltitudeReference.AGL)

    def test_characters_outside_the_alphabet_are_rejected(self) -> None:
        with pytest.raises(CellDomainError, match="alphabet"):
            Cell3D.parse_token(SPEC, "abcdei.00", reference=AltitudeReference.AGL)

    def test_a_horizontal_part_longer_than_the_grid_is_rejected(self) -> None:
        with pytest.raises(CellDomainError):
            Cell3D.parse_token(SPEC, "aaaaaaaa.00", reference=AltitudeReference.AGL)

    def test_volume_cell_token_is_a_legal_resource_identifier(self) -> None:
        """The token survives the resource-key alphabet: no colon, no pipe, no space."""
        cell = SPEC.volume_cell_of(123_456, -98_765, 45_000, level=6, reference=AltitudeReference.AGL)
        key = resource_key("volume_cell", cell.token(), window=Interval.of(1_000, 2_000))
        assert key.canonical == f"volume:{cell.token()}:1000..2000"
        assert ResourceKey.parse(key.canonical).identifier == cell.token()


class TestVolumeCellNeighbourhoods:
    def test_three_dimensional_enumeration_is_symmetric_and_ordered(self) -> None:
        cell = SPEC.volume_cell_of(0, 0, 0, level=6, reference=AltitudeReference.AGL)
        found = cell.neighbourhood(radius=1)
        assert len(found) == 9
        assert cell in found
        assert found == tuple(sorted(found, key=Cell3D.sort_key))
        for neighbour in found:
            assert cell in neighbour.neighbourhood(radius=1)

    def test_ring_excludes_self(self) -> None:
        cell = SPEC.volume_cell_of(0, 0, 0, level=6, reference=AltitudeReference.AGL)
        ring = cell.ring(radius=1)
        assert len(ring) == 8
        assert cell not in ring
        with pytest.raises(CellDomainError):
            cell.ring(radius=0)

    def test_band_is_recoverable_as_an_altitude_band(self) -> None:
        cell = SPEC.volume_cell_of(0, 0, 45_000, level=6, reference=AltitudeReference.AGL)
        band = cell.band()
        assert isinstance(band, AltitudeBand)
        assert band == AltitudeBand(AltitudeReference.AGL, Millimetre(40_000), Millimetre(50_000))
        assert band.reference is AltitudeReference.AGL

    def test_covers_cell_only_for_a_finer_or_equal_child(self) -> None:
        fine = SPEC.volume_cell_of(100_000, -100_000, 0, level=6, reference=AltitudeReference.AMSL)
        coarse = fine.parent()
        assert coarse.covers_cell(fine)
        assert not fine.covers_cell(coarse)

    def test_merge_by_token_is_deterministic_and_distinct(self) -> None:
        fine = SPEC.volume_cell_of(100_000, -100_000, 0, level=6, reference=AltitudeReference.AMSL)
        merged = merge_cells_by_token([fine, fine.parent(), fine])
        assert merged == merge_cells_by_token(list(reversed([fine, fine.parent(), fine])))
        assert len(merged) == 2

    def test_cell_sequence_disjointness_self_check(self) -> None:
        fine = SPEC.volume_cell_of(100_000, -100_000, 0, level=6, reference=AltitudeReference.AMSL)
        assert cell_sequence_is_disjoint([fine, fine.ring(radius=1)[0]])
        assert not cell_sequence_is_disjoint([fine, fine.parent()])
