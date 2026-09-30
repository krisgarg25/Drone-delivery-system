"""Unit tests for canonical resource keys and the single lock order."""

from __future__ import annotations

import itertools

import pytest

from droneops_geom_core import (
    INT64_MICROSECOND_MAX,
    INT64_MICROSECOND_MIN,
    Interval,
    IntervalError,
    ResourceKey,
    ResourceKeyError,
    ResourceKind,
    kind_from_token,
    lock_order,
    resource_key,
    sorted_resource_keys,
    validate_identifier,
)

WINDOW = Interval.of(1_000_000, 2_000_000)


def every_kind_window() -> list[ResourceKey]:
    keys: list[ResourceKey] = []
    for kind in ResourceKind:
        if kind.takes_window:
            keys.append(resource_key(kind, f"id-{kind.name.lower()}", window=WINDOW))
        else:
            keys.append(resource_key(kind, "id-corridor"))
    return keys


class TestCanonicalForms:
    def test_the_six_forms_are_exactly_the_architecture_document(self) -> None:
        assert resource_key(ResourceKind.CORRIDOR, "SEG-12").canonical == "corridor:SEG-12"
        assert (
            resource_key(ResourceKind.VOLUME_CELL, "ezskp1.00", window=WINDOW).canonical
            == "volume:ezskp1.00:1000000..2000000"
        )
        assert (
            resource_key(ResourceKind.PAD, "HUB-01", window=WINDOW).canonical
            == "pad:HUB-01:1000000..2000000"
        )
        assert (
            resource_key(ResourceKind.CHARGER_BAY, "BAY-3", window=WINDOW).canonical
            == "charger:BAY-3:1000000..2000000"
        )
        assert (
            resource_key(ResourceKind.CREW, "PILOT-7", window=WINDOW).canonical
            == "crew:PILOT-7:1000000..2000000"
        )
        assert (
            resource_key(ResourceKind.CROSSING, "X-N4", window=WINDOW).canonical
            == "crossing:X-N4:1000000..2000000"
        )

    def test_kind_names_map_to_architecture_prefixes(self) -> None:
        assert ResourceKind.VOLUME_CELL.value == "volume"
        assert ResourceKind.CHARGER_BAY.value == "charger"
        assert kind_from_token("volume") is ResourceKind.VOLUME_CELL
        assert kind_from_token("charger") is ResourceKind.CHARGER_BAY
        assert kind_from_token("volume_cell") is ResourceKind.VOLUME_CELL
        assert kind_from_token("charger_bay") is ResourceKind.CHARGER_BAY

    def test_an_unknown_kind_is_rejected(self) -> None:
        for bad in ("sidewalk", "VOLUME", "", "corridor "):
            with pytest.raises(ResourceKeyError, match="unknown resource kind"):
                kind_from_token(bad)

    def test_negative_window_endpoints_are_canonical(self) -> None:
        key = resource_key(ResourceKind.PAD, "HUB-01", window=Interval.of(-5_000, -1_000))
        assert key.canonical == "pad:HUB-01:-5000..-1000"
        assert ResourceKey.parse(key.canonical) == key

    def test_a_corridor_must_not_carry_a_window(self) -> None:
        with pytest.raises(ResourceKeyError, match="no window"):
            resource_key(ResourceKind.CORRIDOR, "SEG-12", window=WINDOW)

    def test_a_time_bounded_kind_must_carry_a_window(self) -> None:
        for kind in ResourceKind:
            if kind.takes_window:
                with pytest.raises(ResourceKeyError, match="time-bounded"):
                    resource_key(kind, "somewhere")

    def test_an_empty_window_claims_nothing(self) -> None:
        with pytest.raises(ResourceKeyError, match="at least one instant"):
            resource_key(ResourceKind.PAD, "HUB-01", window=Interval.of(5, 5))


class TestIdentifiers:
    def test_empty_identifier_is_rejected(self) -> None:
        with pytest.raises(ResourceKeyError, match="must not be empty"):
            resource_key(ResourceKind.PAD, "", window=WINDOW)

    @pytest.mark.parametrize(
        "bad", ["has space", "has:colon", "has|pipe", "has\ttab", "x" * 65, "has\nnewline"]
    )
    def test_illegal_identifiers_are_rejected(self, bad: str) -> None:
        with pytest.raises(ResourceKeyError):
            validate_identifier(bad)

    @pytest.mark.parametrize("good", ["SEG-12", "hub_01", "a.b.c", "tilde~1", "Camel42", "x" * 64])
    def test_legal_identifiers_pass(self, good: str) -> None:
        assert validate_identifier(good) == good

    def test_parse_rejects_a_stored_key_with_an_illegal_identifier(self) -> None:
        with pytest.raises(ResourceKeyError):
            ResourceKey.parse("pad:bad identifier:1000..2000")


class TestParsing:
    def test_round_trip_is_idempotent_for_every_kind(self) -> None:
        for key in every_kind_window():
            parsed = ResourceKey.parse(key.canonical)
            assert parsed == key
            assert parsed.canonical == key.canonical
            assert ResourceKey.parse(parsed.canonical).canonical == key.canonical

    def test_wrong_field_counts_are_rejected(self) -> None:
        for bad in ("pad:HUB-01", "pad:HUB-01:1:2", "corridor:SEG-1:2", "", "pad"):
            with pytest.raises(ResourceKeyError):
                ResourceKey.parse(bad)

    def test_a_non_integer_window_is_rejected(self) -> None:
        with pytest.raises(ResourceKeyError, match="integer microseconds"):
            ResourceKey.parse("pad:HUB-01:1000..2000.5")

    def test_a_reversed_window_is_rejected_on_parse(self) -> None:
        with pytest.raises(IntervalError, match="precedes"):
            ResourceKey.parse("pad:HUB-01:2000..1000")

    def test_a_key_with_a_window_on_a_corridor_is_rejected_on_parse(self) -> None:
        with pytest.raises(ResourceKeyError, match="must not carry a time window"):
            ResourceKey.parse("corridor:SEG-12:1000..2000")

    def test_parse_is_the_only_normaliser_and_still_validates(self) -> None:
        assert ResourceKey.parse("charger:BAY-3:0..1").kind is ResourceKind.CHARGER_BAY


class TestLockOrder:
    def test_kind_rank_order_is_the_documented_pipeline_order(self) -> None:
        assert [kind.lock_rank for kind in ResourceKind] == [0, 1, 2, 3, 4, 5]
        assert [kind.name for kind in ResourceKind] == [
            "CORRIDOR",
            "VOLUME_CELL",
            "CROSSING",
            "PAD",
            "CHARGER_BAY",
            "CREW",
        ]

    def test_sorting_is_independent_of_input_order(self) -> None:
        keys = every_kind_window()
        forward = sorted_resource_keys(keys)
        for permutation in itertools.permutations(keys):
            assert sorted_resource_keys(permutation) == forward

    def test_lock_token_order_equals_sort_key_order(self) -> None:
        keys = every_kind_window() + [
            resource_key(ResourceKind.PAD, "HUB-02", window=WINDOW),
            resource_key(ResourceKind.PAD, "HUB-02", window=Interval.of(0, 1)),
            resource_key(ResourceKind.VOLUME_CELL, "ezskp1.00", window=Interval.of(1, 2)),
        ]
        ordered = sorted_resource_keys(keys)
        assert [key.lock_token() for key in ordered] == sorted(key.lock_token() for key in ordered)
        assert [key.sort_key() for key in ordered] == sorted(key.sort_key() for key in ordered)

    def test_padding_makes_identifier_ordering_pure_lexicographic(self) -> None:
        """``ab`` must sort before ``abc`` in both the tuple and the token order."""
        short = resource_key(ResourceKind.PAD, "ab", window=WINDOW)
        long = resource_key(ResourceKind.PAD, "abc", window=WINDOW)
        assert short < long
        assert short.lock_token() < long.lock_token()
        assert sorted([long, short]) == [short, long]

    def test_duplicates_are_collapsed_once(self) -> None:
        key = resource_key(ResourceKind.PAD, "HUB-01", window=WINDOW)
        assert sorted_resource_keys([key, key, key]) == (key,)
        assert len(lock_order([key, key])) == 1

    def test_the_order_is_total_and_antisymmetric(self) -> None:
        keys = every_kind_window()
        for left, right in itertools.permutations(keys, 2):
            assert (left < right) != (right < left)
            assert left <= right or right <= left

    def test_comparison_against_a_foreign_type_is_a_type_error(self) -> None:
        with pytest.raises(TypeError):
            resource_key(ResourceKind.CORRIDOR, "A") < "corridor:A"  # noqa: B015

    def test_window_ordering_uses_start_then_end(self) -> None:
        early = resource_key(ResourceKind.PAD, "H", window=Interval.of(0, 10))
        late = resource_key(ResourceKind.PAD, "H", window=Interval.of(5, 6))
        assert early < late
        assert resource_key(ResourceKind.PAD, "H", window=Interval.of(5, 7)) > late

    def test_domain_edges_order_correctly(self) -> None:
        min_window = resource_key(
            ResourceKind.PAD, "H", window=Interval.of(INT64_MICROSECOND_MIN, 0)
        )
        max_window = resource_key(
            ResourceKind.PAD, "H", window=Interval.of(0, INT64_MICROSECOND_MAX)
        )
        assert min_window < max_window
        assert ResourceKey.parse(min_window.canonical) == min_window
        assert ResourceKey.parse(max_window.canonical) == max_window

    def test_lock_token_is_stable_width(self) -> None:
        width = len(resource_key(ResourceKind.PAD, "a", window=WINDOW).lock_token())
        assert width == len(resource_key(ResourceKind.CREW, "a" * 64, window=WINDOW).lock_token())
        assert width == 2 + 1 + 64 + 1 + 20 + 1 + 20

    def test_helpers(self) -> None:
        key = resource_key(ResourceKind.PAD, "HUB-01", window=WINDOW)
        assert key.identity_token() == "pad:HUB-01"
        # The identity alone is not a valid reservation key, and cannot become one.
        with pytest.raises(ResourceKeyError, match="time-bounded"):
            ResourceKey(ResourceKind.PAD, "HUB-01", None)
        assert key.with_window(Interval.of(0, 5)).window == Interval.of(0, 5)
        with pytest.raises(ResourceKeyError):
            resource_key(ResourceKind.CORRIDOR, "A").with_window(Interval.of(0, 5))

    def test_str_is_the_canonical_form(self) -> None:
        key = resource_key(ResourceKind.PAD, "HUB-01", window=WINDOW)
        assert str(key) == key.canonical
