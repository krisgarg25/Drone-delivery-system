"""Unit tests for half-open interval semantics and the interval algebra.

The edge cases are the specification: adjacency must not be overlap, an empty
interval must never block anything, a single instant is ``[p, p+1)``, and the
algebra must close and conserve measure.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from droneops_geom_core import (
    INVERSE_RELATION,
    Interval,
    IntervalError,
    RangeRelation,
    TimeDomainError,
    gaps_within,
    intersect_all,
    merge,
    overlaps_any,
    shift_all,
    sort_key,
    subtract,
    total_measure,
)


def iv(start: int, end: int) -> Interval:
    return Interval.of(start, end)


class TestConstruction:
    def test_end_before_start_is_rejected(self) -> None:
        with pytest.raises(IntervalError, match="precedes"):
            iv(10, 9)

    def test_equal_endpoints_are_a_legal_empty_interval(self) -> None:
        empty = iv(5, 5)
        assert empty.is_empty
        assert empty.measure == 0

    def test_reversed_interval_is_never_normalised(self) -> None:
        """Silently swapping endpoints would turn a caller bug into a wrong claim."""
        with pytest.raises(IntervalError):
            iv(-3, -4)

    def test_negative_time_is_accepted(self) -> None:
        window = iv(-1_000, -500)
        assert window.measure == 500
        assert window.contains_instant(-800)

    def test_non_integer_endpoints_are_rejected(self) -> None:
        with pytest.raises(TimeDomainError):
            Interval(1.5, 2.5)  # type: ignore[arg-type]

    def test_float_is_rejected_even_where_an_int_is_expected(self) -> None:
        with pytest.raises(TimeDomainError):
            iv(0, 1_000_000.0)  # type: ignore[arg-type]

    def test_empty_at_is_the_anchored_empty_interval(self) -> None:
        assert Interval.empty_at(7) == iv(7, 7)

    def test_from_datetimes_converts_at_the_boundary(self) -> None:
        start = datetime(2026, 9, 30, tzinfo=UTC)
        end = start + timedelta(minutes=1)
        window = Interval.from_datetimes(start, end)
        assert window.measure == 60_000_000
        assert window.as_datetimes() == (start, end)

    def test_from_datetimes_rejects_naive_input(self) -> None:
        with pytest.raises(TimeDomainError):
            Interval.from_datetimes(datetime(2026, 9, 30), datetime(2026, 9, 30, 1))


class TestPredicates:
    def test_adjacency_is_not_overlap(self) -> None:
        assert not iv(0, 5).overlaps(iv(5, 9))
        assert not iv(5, 9).overlaps(iv(0, 5))

    def test_empty_interval_overlaps_nothing(self) -> None:
        """The Phase 0 stub got this wrong; it is a regression test, not a nicety."""
        assert not iv(5, 5).overlaps(iv(0, 10))
        assert not iv(0, 10).overlaps(iv(5, 5))

    def test_genuine_overlap(self) -> None:
        assert iv(0, 10).overlaps(iv(5, 15))
        assert iv(0, 10).overlaps(iv(2, 3))
        assert not iv(0, 10).overlaps(iv(10, 20))

    def test_single_instant_is_half_open_not_closed(self) -> None:
        point = iv(4, 5)
        assert point.is_single_instant
        assert point.contains_instant(4)
        assert not point.contains_instant(5)
        assert point.measure == 1

    def test_contains_interval(self) -> None:
        assert iv(0, 10).contains(iv(2, 8))
        assert iv(0, 10).contains(iv(0, 10))
        assert not iv(0, 10).contains(iv(0, 11))

    def test_contains_an_empty_interval_only_when_its_anchor_is_inside(self) -> None:
        assert iv(0, 10).contains(iv(5, 5))
        assert not iv(0, 4).contains(iv(5, 5))

    def test_contains_instant(self) -> None:
        assert iv(0, 10).contains_instant(0)
        assert not iv(0, 10).contains_instant(10)
        assert not iv(5, 5).contains_instant(5)

    def test_touches_requires_both_non_empty(self) -> None:
        assert iv(0, 5).touches(iv(5, 9))
        assert iv(5, 9).touches(iv(0, 5))
        assert not iv(0, 5).touches(iv(5, 5))
        assert not iv(0, 5).touches(iv(6, 9))

    def test_touching_is_not_overlapping(self) -> None:
        both = iv(0, 5), iv(5, 9)
        assert both[0].touches(both[1])
        assert not both[0].overlaps(both[1])


class TestAlgebra:
    def test_intersection_is_a_canonical_zero_or_one_tuple(self) -> None:
        assert iv(0, 10).intersection(iv(5, 15)) == (iv(5, 10),)
        assert iv(0, 5).intersection(iv(5, 10)) == ()
        assert iv(5, 5).intersection(iv(0, 10)) == ()

    def test_union_merges_touching_and_keeps_disjoint_apart(self) -> None:
        assert iv(0, 5).union(iv(5, 9)) == (iv(0, 9),)
        assert iv(0, 9).union(iv(5, 6)) == (iv(0, 9),)
        assert iv(0, 5).union(iv(6, 9)) == (iv(0, 5), iv(6, 9))

    def test_union_with_nothing_empty(self) -> None:
        assert iv(0, 0).union(iv(0, 0)) == ()
        assert iv(0, 0).union(iv(2, 4)) == (iv(2, 4),)

    def test_difference_pieces(self) -> None:
        assert iv(0, 10).difference(iv(3, 5)) == (iv(0, 3), iv(5, 10))
        assert iv(0, 10).difference(iv(-5, 20)) == ()
        assert iv(0, 10).difference(iv(0, 3)) == (iv(3, 10),)
        assert iv(0, 10).difference(iv(10, 20)) == (iv(0, 10),)

    def test_subtract_many_in_one_pass(self) -> None:
        base = iv(0, 100)
        others = [iv(10, 20), iv(15, 30), iv(95, 200), iv(-40, -5)]
        assert base.subtract(others) == (iv(0, 10), iv(30, 95))

    def test_subtract_drops_empty_base(self) -> None:
        assert iv(5, 5).subtract([iv(0, 10)]) == ()

    def test_subtract_on_adjacency_does_not_nibble_an_endpoint(self) -> None:
        assert iv(0, 5).subtract([iv(5, 6)]) == (iv(0, 5),)

    def test_shift_moves_both_endpoints(self) -> None:
        assert iv(0, 5).shifted_by(10) == iv(10, 15)
        assert iv(0, 5).shifted_by(-10) == iv(-10, -5)
        assert iv(3, 3).shifted_by(7) == iv(10, 10)

    def test_shift_rejects_leaving_the_int64_domain(self) -> None:
        from droneops_geom_core import INT64_MICROSECOND_MAX

        with pytest.raises(TimeDomainError):
            iv(INT64_MICROSECOND_MAX - 1, INT64_MICROSECOND_MAX).shifted_by(5)

    def test_shift_all_preserves_ascending_order(self) -> None:
        shifted = shift_all([iv(9, 10), iv(0, 1), iv(4, 5)], 100)
        assert shifted == (iv(100, 101), iv(104, 105), iv(109, 110))

    def test_gap_to_reports_the_room_between(self) -> None:
        assert iv(0, 5).gap_to(iv(9, 12)) == iv(5, 9)
        assert iv(9, 12).gap_to(iv(0, 5)) == iv(5, 9)

    def test_gap_to_is_none_for_touching_and_overlapping(self) -> None:
        assert iv(0, 5).gap_to(iv(5, 9)) is None
        assert iv(0, 7).gap_to(iv(5, 9)) is None

    def test_clipped_to_is_intersection(self) -> None:
        assert iv(-5, 5).clipped_to(iv(0, 100)) == (iv(0, 5),)


class TestMergeAndAggregates:
    def test_merge_is_order_independent(self) -> None:
        feed = [iv(20, 30), iv(0, 10), iv(5, 6), iv(10, 12)]
        assert merge(feed) == merge(list(reversed(feed))) == (iv(0, 12), iv(20, 30))

    def test_merge_joins_touching_runs(self) -> None:
        assert merge([iv(0, 5), iv(5, 10), iv(10, 15)]) == (iv(0, 15),)

    def test_merge_drops_empties(self) -> None:
        assert merge([iv(3, 3), iv(0, 2)]) == (iv(0, 2),)
        assert merge([]) == ()

    def test_total_measure_counts_overlap_once(self) -> None:
        assert total_measure([iv(0, 10), iv(5, 15)]) == 15
        assert total_measure([iv(0, 10), iv(0, 10)]) == 10

    def test_intersect_all(self) -> None:
        assert intersect_all([iv(0, 10), iv(3, 20), iv(5, 8)]) == iv(5, 8)
        assert intersect_all([iv(0, 5), iv(6, 9)]) is None
        assert intersect_all([]) is None
        assert intersect_all([iv(0, 10), iv(4, 4)]) is None

    def test_gaps_within_is_the_free_set(self) -> None:
        assert gaps_within(iv(0, 100), [iv(10, 20), iv(15, 30)]) == (iv(0, 10), iv(30, 100))

    def test_overlaps_any_returns_the_blocker(self) -> None:
        occupied = [iv(40, 50), iv(10, 20)]
        assert overlaps_any(iv(15, 18), occupied) == iv(10, 20)
        assert overlaps_any(iv(25, 30), occupied) is None

    def test_sort_key_is_start_then_end(self) -> None:
        assert sorted([iv(5, 6), iv(5, 5), iv(1, 9)], key=sort_key) == [
            iv(1, 9),
            iv(5, 5),
            iv(5, 6),
        ]


class TestAllenRelations:
    def test_the_thirteen_relations_are_all_reachable_and_named(self) -> None:
        cases = {
            RangeRelation.BEFORE: (iv(0, 1), iv(2, 3)),
            RangeRelation.MEETS: (iv(0, 1), iv(1, 2)),
            RangeRelation.OVERLAPS: (iv(0, 5), iv(3, 8)),
            RangeRelation.STARTS: (iv(0, 3), iv(0, 5)),
            RangeRelation.DURING: (iv(2, 4), iv(0, 10)),
            RangeRelation.FINISHES: (iv(3, 5), iv(0, 5)),
            RangeRelation.EQUALS: (iv(0, 5), iv(0, 5)),
            RangeRelation.AFTER: (iv(2, 3), iv(0, 1)),
            RangeRelation.MET_BY: (iv(1, 2), iv(0, 1)),
            RangeRelation.OVERLAPPED_BY: (iv(3, 8), iv(0, 5)),
            RangeRelation.STARTED_BY: (iv(0, 5), iv(0, 3)),
            RangeRelation.CONTAINS: (iv(0, 10), iv(2, 4)),
            RangeRelation.FINISHED_BY: (iv(0, 5), iv(3, 5)),
        }
        assert set(cases) == set(RangeRelation)
        for expected, (left, right) in cases.items():
            assert left.relation_to(right) is expected

    def test_relations_are_exactly_invertible(self) -> None:
        pairs = [(iv(0, 4), iv(2, 9)), (iv(0, 4), iv(4, 9)), (iv(0, 4), iv(1, 2))]
        for left, right in pairs:
            forward = left.relation_to(right)
            assert right.relation_to(left) is INVERSE_RELATION[forward]

    def test_empty_operand_has_no_invented_relation(self) -> None:
        with pytest.raises(IntervalError, match="non-empty"):
            iv(5, 5).relation_to(iv(0, 10))

    def test_relation_agrees_with_overlaps_and_touches(self) -> None:
        overlapping = {RangeRelation.OVERLAPS, RangeRelation.OVERLAPPED_BY, RangeRelation.STARTS,
                       RangeRelation.STARTED_BY, RangeRelation.DURING, RangeRelation.CONTAINS,
                       RangeRelation.FINISHES, RangeRelation.FINISHED_BY, RangeRelation.EQUALS}
        for relation in RangeRelation:
            left, right = {
                RangeRelation.BEFORE: (iv(0, 1), iv(2, 3)),
                RangeRelation.AFTER: (iv(2, 3), iv(0, 1)),
                RangeRelation.MEETS: (iv(0, 1), iv(1, 2)),
                RangeRelation.MET_BY: (iv(1, 2), iv(0, 1)),
                RangeRelation.OVERLAPS: (iv(0, 5), iv(3, 8)),
                RangeRelation.OVERLAPPED_BY: (iv(3, 8), iv(0, 5)),
                RangeRelation.STARTS: (iv(0, 3), iv(0, 5)),
                RangeRelation.STARTED_BY: (iv(0, 5), iv(0, 3)),
                RangeRelation.DURING: (iv(2, 4), iv(0, 10)),
                RangeRelation.CONTAINS: (iv(0, 10), iv(2, 4)),
                RangeRelation.FINISHES: (iv(3, 5), iv(0, 5)),
                RangeRelation.FINISHED_BY: (iv(0, 5), iv(3, 5)),
                RangeRelation.EQUALS: (iv(0, 5), iv(0, 5)),
            }[relation]
            assert left.overlaps(right) is (relation in overlapping)
            assert left.touches(right) is (
                relation in {RangeRelation.MEETS, RangeRelation.MET_BY}
            )


class TestHugeSpans:
    def test_a_domain_spanning_the_int64_window_still_computes(self) -> None:
        from droneops_geom_core import INT64_MICROSECOND_MAX, INT64_MICROSECOND_MIN

        whole = iv(INT64_MICROSECOND_MIN, INT64_MICROSECOND_MAX)
        assert whole.measure == INT64_MICROSECOND_MAX - INT64_MICROSECOND_MIN
        assert subtract(whole, [iv(0, 1)]) == (
            iv(INT64_MICROSECOND_MIN, 0),
            iv(1, INT64_MICROSECOND_MAX),
        )

    def test_equality_and_hashing_use_both_endpoints(self) -> None:
        assert iv(0, 5) == iv(0, 5)
        assert iv(0, 5) != iv(0, 6)
        assert len({iv(0, 5), iv(0, 5), iv(1, 5)}) == 2
