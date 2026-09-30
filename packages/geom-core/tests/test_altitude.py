"""Unit tests for altitude references, vertical datums and altitude bands.

Invariant I9 is the subject: an altitude without a reference does not exist here,
and a conversion without the datum it needs raises instead of guessing.
"""

from __future__ import annotations

import pytest

from droneops_geom_core import (
    ALTITUDE_REFERENCE_ORDER,
    QUANTIZATION_TOLERANCE_MM,
    Altitude,
    AltitudeBand,
    AltitudeReference,
    AltitudeReferenceError,
    GeomCoreError,
    Millimetre,
    VerticalDatums,
    band_sequence_is_disjoint,
    merge_bands,
    reference_from_token,
    subtract_band,
)

DATUMS = VerticalDatums(
    terrain_amsl_mm=Millimetre(320_000),
    site_datum_amsl_mm=Millimetre(310_000),
    pad_deck_amsl_mm=Millimetre(322_500),
)


class TestReferences:
    def test_all_five_references_are_named(self) -> None:
        assert {reference.value for reference in AltitudeReference} == {
            "AGL",
            "AMSL",
            "GROUND",
            "PAD",
            "UNSPECIFIED",
        }
        assert len(ALTITUDE_REFERENCE_ORDER) == 5

    def test_reference_tokens_parse_case_sensitively(self) -> None:
        assert reference_from_token("AGL") is AltitudeReference.AGL
        with pytest.raises(AltitudeReferenceError):
            reference_from_token("agl")
        with pytest.raises(AltitudeReferenceError):
            reference_from_token("MSL")

    def test_unspecified_cannot_carry_a_height(self) -> None:
        with pytest.raises(AltitudeReferenceError, match="UNSPECIFIED"):
            Altitude(AltitudeReference.UNSPECIFIED, Millimetre(100_000))

    def test_unspecified_band_is_rejected_too(self) -> None:
        with pytest.raises(AltitudeReferenceError):
            AltitudeBand(AltitudeReference.UNSPECIFIED, Millimetre(0), Millimetre(100_000))


class TestConversions:
    def test_agl_to_amsl_uses_the_terrain_under_the_aircraft(self) -> None:
        height = Altitude(AltitudeReference.AGL, Millimetre(120_000))
        assert height.as_amsl(DATUMS).value_mm == 440_000

    def test_ground_uses_the_site_datum_not_the_terrain(self) -> None:
        """AGL and GROUND differ exactly where the terrain differs from the datum."""
        height = Altitude(AltitudeReference.AGL, Millimetre(120_000))
        ground = height.as_reference(AltitudeReference.GROUND, DATUMS)
        assert ground.value_mm == 130_000
        flat = height.as_reference(AltitudeReference.GROUND, DATUMS.flat())
        assert flat.value_mm == height.as_reference(AltitudeReference.AGL, DATUMS.flat()).value_mm

    def test_pad_uses_the_surveyed_deck(self) -> None:
        height = Altitude(AltitudeReference.AGL, Millimetre(120_000))
        assert height.as_reference(AltitudeReference.PAD, DATUMS).value_mm == 117_500

    def test_conversion_returns_a_reference_with_its_value(self) -> None:
        converted = Altitude(AltitudeReference.AMSL, Millimetre(440_000)).as_reference(
            AltitudeReference.AGL, DATUMS
        )
        assert converted.reference is AltitudeReference.AGL
        assert converted.value_mm == 120_000

    def test_round_trip_back_to_the_original_reference_is_exact(self) -> None:
        original = Altitude(AltitudeReference.AGL, Millimetre(123_456))
        back = original.as_amsl(DATUMS).as_reference(AltitudeReference.AGL, DATUMS)
        assert back == original

    def test_converting_to_unspecified_is_refused(self) -> None:
        with pytest.raises(AltitudeReferenceError, match="UNSPECIFIED"):
            Altitude(AltitudeReference.AGL, Millimetre(1)).as_reference(
                AltitudeReference.UNSPECIFIED, DATUMS
            )

    def test_missing_pad_datum_raises_instead_of_assuming_ground_level(self) -> None:
        no_deck = VerticalDatums(Millimetre(300_000), Millimetre(300_000), None)
        with pytest.raises(AltitudeReferenceError, match="pad_deck_amsl_mm"):
            Altitude(AltitudeReference.AGL, Millimetre(100_000)).as_reference(
                AltitudeReference.PAD, no_deck
            )

    def test_a_pad_altitude_cannot_be_read_without_the_deck_either(self) -> None:
        no_deck = VerticalDatums(Millimetre(300_000), Millimetre(300_000), None)
        with pytest.raises(AltitudeReferenceError):
            Altitude(AltitudeReference.PAD, Millimetre(100_000)).as_amsl(no_deck)

    def test_separation_across_references_needs_datums(self) -> None:
        above_pad = Altitude(AltitudeReference.PAD, Millimetre(60_000))
        terrain_hut = Altitude(AltitudeReference.AGL, Millimetre(58_000))
        assert above_pad.separation_from(terrain_hut, DATUMS) == 4_500

    def test_metre_boundary_carries_the_stated_tolerance(self) -> None:
        height = Altitude.in_reference(117.5, AltitudeReference.AGL)
        assert height.value_mm == 117_500
        assert abs(height.value_m - 117.5) < QUANTIZATION_TOLERANCE_MM / 1000.0


class TestAltitudeKeys:
    def test_key_round_trip(self) -> None:
        height = Altitude(AltitudeReference.GROUND, Millimetre(-250))
        assert Altitude.from_key(height.key()) == height
        assert Altitude.from_key(height.key()).key() == height.key()

    def test_malformed_keys_are_rejected(self) -> None:
        for bad in ("alt|AGL", "alt|AGL|12.5", "band|AGL|1", "alt|AGL|1|2", "nope|AGL|1"):
            with pytest.raises(AltitudeReferenceError):
                Altitude.from_key(bad)

    def test_unspecified_cannot_be_reconstructed_from_a_key(self) -> None:
        with pytest.raises(AltitudeReferenceError):
            Altitude.from_key("alt|UNSPECIFIED|0")


class TestVerticalDatums:
    def test_at_site_defaults_the_datum_to_the_terrain(self) -> None:
        datums = VerticalDatums.at_site(350.0)
        assert datums.terrain_amsl_mm == 350_000
        assert datums.site_datum_amsl_mm == 350_000
        assert datums.pad_deck_amsl_mm is None

    def test_at_site_keeps_a_surveyed_pad_deck_and_site_datum(self) -> None:
        datums = VerticalDatums.at_site(350.0, 349.5, 350.25)
        assert (datums.terrain_amsl_mm, datums.site_datum_amsl_mm, datums.pad_deck_amsl_mm) == (
            350_000,
            349_500,
            350_250,
        )

    def test_flat_collapse_is_explicit(self) -> None:
        flat = DATUMS.flat()
        assert flat.site_datum_amsl_mm == DATUMS.terrain_amsl_mm
        assert flat.pad_deck_amsl_mm == DATUMS.terrain_amsl_mm

    def test_amsl_offset_is_zero(self) -> None:
        assert DATUMS.offset_for(AltitudeReference.AMSL) == 0

    def test_non_integer_datum_is_rejected(self) -> None:
        with pytest.raises(GeomCoreError, match="millimetre"):
            VerticalDatums(Millimetre(1.5), Millimetre(0), None)  # type: ignore[arg-type]


class TestBands:
    def test_band_measure_and_emptiness(self) -> None:
        band = AltitudeBand(AltitudeReference.AGL, Millimetre(40_000), Millimetre(120_000))
        assert band.measure == 80_000
        assert not band.is_empty
        assert AltitudeBand(AltitudeReference.AGL, Millimetre(40_000), Millimetre(40_000)).is_empty

    def test_reversed_band_is_rejected(self) -> None:
        with pytest.raises(AltitudeReferenceError, match="reversed"):
            AltitudeBand(AltitudeReference.AGL, Millimetre(120_000), Millimetre(40_000))

    def test_band_edges_are_half_open(self) -> None:
        band = AltitudeBand(AltitudeReference.AGL, Millimetre(0), Millimetre(120_000))
        assert band.contains_altitude(Altitude(AltitudeReference.AGL, Millimetre(120_000)), DATUMS) is False
        assert band.contains_altitude(Altitude(AltitudeReference.AGL, Millimetre(0)), DATUMS) is True

    def test_contains_altitude_across_references(self) -> None:
        band = AltitudeBand(AltitudeReference.AMSL, Millimetre(400_000), Millimetre(450_000))
        inside = Altitude(AltitudeReference.AGL, Millimetre(100_000))
        assert band.contains_altitude(inside, DATUMS)

    def test_adjacent_bands_do_not_intersect(self) -> None:
        lower = AltitudeBand(AltitudeReference.AGL, Millimetre(0), Millimetre(100_000))
        upper = AltitudeBand(AltitudeReference.AGL, Millimetre(100_000), Millimetre(200_000))
        assert not lower.intersects(upper)
        assert lower.union(upper) == (
            AltitudeBand(AltitudeReference.AGL, Millimetre(0), Millimetre(200_000)),
        )

    def test_intersection_and_difference(self) -> None:
        a = AltitudeBand(AltitudeReference.AGL, Millimetre(0), Millimetre(100_000))
        b = AltitudeBand(AltitudeReference.AGL, Millimetre(60_000), Millimetre(200_000))
        assert a.intersection(b) == (
            AltitudeBand(AltitudeReference.AGL, Millimetre(60_000), Millimetre(100_000)),
        )
        assert a.difference(b) == (
            AltitudeBand(AltitudeReference.AGL, Millimetre(0), Millimetre(60_000)),
        )

    def test_cross_reference_band_arithmetic_is_refused(self) -> None:
        agl = AltitudeBand(AltitudeReference.AGL, Millimetre(0), Millimetre(100_000))
        amsl = AltitudeBand(AltitudeReference.AMSL, Millimetre(0), Millimetre(100_000))
        with pytest.raises(AltitudeReferenceError):
            agl.intersects(amsl)
        with pytest.raises(AltitudeReferenceError):
            merge_bands([agl, amsl])

    def test_from_altitudes_requires_one_reference(self) -> None:
        with pytest.raises(AltitudeReferenceError, match="share a reference"):
            AltitudeBand.from_altitudes(
                Altitude(AltitudeReference.AGL, Millimetre(0)),
                Altitude(AltitudeReference.AMSL, Millimetre(100_000)),
            )

    def test_shift_and_grow(self) -> None:
        band = AltitudeBand(AltitudeReference.AGL, Millimetre(40_000), Millimetre(120_000))
        assert band.shifted_by(10_000).key() == AltitudeBand(
            AltitudeReference.AGL, Millimetre(50_000), Millimetre(130_000)
        ).key()
        assert band.grow_by(10_000) == AltitudeBand(
            AltitudeReference.AGL, Millimetre(30_000), Millimetre(130_000)
        )
        with pytest.raises(AltitudeReferenceError):
            band.grow_by(-1)

    def test_clamp(self) -> None:
        band = AltitudeBand(AltitudeReference.AGL, Millimetre(40_000), Millimetre(120_000))
        corridor = AltitudeBand(AltitudeReference.AGL, Millimetre(0), Millimetre(90_000))
        assert band.clamp_to(corridor) == AltitudeBand(
            AltitudeReference.AGL, Millimetre(40_000), Millimetre(90_000)
        )
        outside = AltitudeBand(AltitudeReference.AGL, Millimetre(200_000), Millimetre(300_000))
        assert band.clamp_to(outside).is_empty

    def test_band_reference_conversion_moves_both_edges(self) -> None:
        band = AltitudeBand(AltitudeReference.AGL, Millimetre(40_000), Millimetre(120_000))
        absolute = band.as_reference(AltitudeReference.AMSL, DATUMS)
        assert absolute == AltitudeBand(
            AltitudeReference.AMSL, Millimetre(360_000), Millimetre(440_000)
        )
        assert absolute.as_reference(AltitudeReference.AGL, DATUMS) == band

    def test_band_index_and_reconstruction(self) -> None:
        band = AltitudeBand(AltitudeReference.AGL, Millimetre(40_000), Millimetre(50_000))
        assert band.band_index(10_000) == 4
        assert AltitudeBand.covering_index(AltitudeReference.AGL, 4, 10_000) == band
        assert AltitudeBand.covering_index(AltitudeReference.AGL, -1, 10_000).lower_mm == -10_000

    def test_negative_band_index_uses_floor_not_truncation(self) -> None:
        band = AltitudeBand(AltitudeReference.AMSL, Millimetre(-5_000), Millimetre(0))
        assert band.band_index(10_000) == -1

    def test_band_key_round_trip(self) -> None:
        band = AltitudeBand(AltitudeReference.PAD, Millimetre(-1_000), Millimetre(120_000))
        assert AltitudeBand.from_key(band.key()) == band
        assert band.key() == "band|PAD|-1000|120000"

    def test_malformed_band_keys_are_rejected(self) -> None:
        for bad in ("band|AGL|0", "band|AGL|a|b", "alt|AGL|0|10", ""):
            with pytest.raises(AltitudeReferenceError):
                AltitudeBand.from_key(bad)


class TestBandAggregates:
    def test_merge_is_canonical_and_order_independent(self) -> None:
        bands = [
            AltitudeBand(AltitudeReference.AGL, Millimetre(90_000), Millimetre(120_000)),
            AltitudeBand(AltitudeReference.AGL, Millimetre(0), Millimetre(60_000)),
            AltitudeBand(AltitudeReference.AGL, Millimetre(50_000), Millimetre(70_000)),
            AltitudeBand(AltitudeReference.AGL, Millimetre(70_000), Millimetre(70_000)),
        ]
        expected = (
            AltitudeBand(AltitudeReference.AGL, Millimetre(0), Millimetre(70_000)),
            AltitudeBand(AltitudeReference.AGL, Millimetre(90_000), Millimetre(120_000)),
        )
        assert merge_bands(bands) == expected
        assert merge_bands(list(reversed(bands))) == expected

    def test_subtract_many(self) -> None:
        base = AltitudeBand(AltitudeReference.AGL, Millimetre(0), Millimetre(120_000))
        removed = [
            AltitudeBand(AltitudeReference.AGL, Millimetre(10_000), Millimetre(20_000)),
            AltitudeBand(AltitudeReference.AGL, Millimetre(15_000), Millimetre(30_000)),
            AltitudeBand(AltitudeReference.AGL, Millimetre(119_000), Millimetre(200_000)),
        ]
        assert subtract_band(base, removed) == (
            AltitudeBand(AltitudeReference.AGL, Millimetre(0), Millimetre(10_000)),
            AltitudeBand(AltitudeReference.AGL, Millimetre(30_000), Millimetre(119_000)),
        )

    def test_disjointness_self_check(self) -> None:
        stacked = [
            AltitudeBand(AltitudeReference.AGL, Millimetre(0), Millimetre(100_000)),
            AltitudeBand(AltitudeReference.AGL, Millimetre(100_000), Millimetre(200_000)),
        ]
        assert band_sequence_is_disjoint(stacked)
        assert not band_sequence_is_disjoint([stacked[0], stacked[0]])

    def test_closed_at_origin_is_the_usual_ceiling(self) -> None:
        ceiling = AltitudeBand.closed_at_origin(AltitudeReference.AGL, 120_000)
        assert ceiling.measure == 120_000
