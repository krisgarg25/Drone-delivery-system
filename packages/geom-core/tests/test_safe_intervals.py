"""Unit tests for safe-interval computation.

Every numeric expectation is derived from the rule in the module docstring -
``t - b >= e + u`` or ``t + d + b <= s - u`` - and the parametrised case checks
the enumeration against a brute-force scan of every entry instant, because a
safe interval that is wrong by one microsecond is a reservation that overlaps a
commitment.
"""

from __future__ import annotations

import pytest

from droneops_geom_core import (
    INT64_MICROSECOND_MAX,
    INT64_MICROSECOND_MIN,
    EntryStatus,
    GapReason,
    Interval,
    Microsecond,
    OccupiedWindow,
    ResourceKey,
    ResourceKeyError,
    SafeIntervalError,
    SafeIntervalPadding,
    SafeIntervalQuery,
    SafeIntervalResult,
    SafeIntervalStatus,
    TimeDomainError,
    classify_entry,
    compute_safe_intervals,
    describe_result,
    earliest_entry_on_or_after,
    resource_key,
)

ZERO_PADDING = SafeIntervalPadding(Microsecond(0), Microsecond(0))
HORIZON = Interval.of(0, 1_000)


def pad_key(identifier: str = "HUB-01", kind: str = "pad") -> ResourceKey:
    return resource_key(
        kind, identifier, window=Interval.of(0, 10**9) if kind != "corridor" else None
    )


def query(
    occupied: list[tuple[int, int]],
    *,
    traversal: int = 10,
    uncertainty: int = 0,
    buffer_us: int = 0,
    horizon: Interval = HORIZON,
) -> SafeIntervalQuery:
    return SafeIntervalQuery.build(
        pad_key(),
        horizon=horizon,
        traversal_us=traversal,
        padding=SafeIntervalPadding(Microsecond(uncertainty), Microsecond(buffer_us)),
        occupied=[(Interval.of(start, end), f"r{start}-{end}") for start, end in occupied],
    )


def entries(result: SafeIntervalResult) -> list[int]:
    """Flatten returned windows back into individual entry instants."""
    out: list[int] = []
    for window in result.windows:
        out.extend(range(window.entry.start, window.entry.end))
    return out


def brute_force(
    occupied: list[tuple[int, int]],
    traversal: int,
    uncertainty: int,
    buffer_us: int,
    horizon: Interval,
) -> list[int]:
    """Scan every entry instant and apply the rule directly.

    The third conjunct is the representability of the release instant, which is
    part of the rule, not an implementation detail.
    """
    return [
        moment
        for moment in range(horizon.start, horizon.end)
        if moment + traversal + buffer_us <= INT64_MICROSECOND_MAX
        and all(
            moment - buffer_us >= end + uncertainty
            or moment + traversal + buffer_us <= start - uncertainty
            for start, end in occupied
        )
    ]


@pytest.mark.parametrize(
    ("occupied", "traversal", "uncertainty", "buffer_us"),
    [
        ([], 10, 0, 0),
        ([(100, 200)], 10, 0, 0),
        ([(100, 200)], 10, 5, 3),
        ([(0, 500)], 10, 0, 0),
        ([(100, 200), (300, 400)], 25, 7, 11),
        ([(100, 200), (201, 300)], 5, 1, 1),
        ([(0, 1000)], 10, 0, 0),
        ([(990, 1000)], 10, 0, 0),
        ([(0, 10)], 10, 0, 0),
        ([(-80, -20)], 10, 3, 2),
        ([(50, 51)], 1, 0, 0),
        ([(100, 200), (150, 260)], 10, 2, 4),
    ],
)
def test_enumeration_matches_the_definitional_rule(
    occupied: list[tuple[int, int]], traversal: int, uncertainty: int, buffer_us: int
) -> None:
    horizon = Interval.of(-200, 1_200) if any(start < 0 for start, _ in occupied) else HORIZON
    built = query(
        occupied, traversal=traversal, uncertainty=uncertainty, buffer_us=buffer_us, horizon=horizon
    )
    result = compute_safe_intervals(built)
    expected = brute_force(occupied, traversal, uncertainty, buffer_us, horizon)
    assert entries(result) == expected
    assert all(
        classify_entry(built, moment).status is EntryStatus.ADMISSIBLE for moment in expected
    )
    assert all(
        classify_entry(built, moment).status is not EntryStatus.ADMISSIBLE
        for moment in range(horizon.start, horizon.end)
        if moment not in expected
    )


class TestPaddingSemantics:
    def test_uncertainty_blocks_the_entry_side(self) -> None:
        """Admissible while ``t + 10 <= 100 - 20``, then from ``t >= 200 + 20``."""
        result = compute_safe_intervals(query([(100, 200)], traversal=10, uncertainty=20))
        assert entries(result) == list(range(0, 71)) + list(range(220, 1_000))

    def test_transition_buffer_blocks_the_exit_side(self) -> None:
        result = compute_safe_intervals(query([(100, 200)], traversal=10, buffer_us=20))
        assert entries(result) == list(range(0, 71)) + list(range(220, 1_000))

    def test_the_two_paddings_are_additive_and_separately_configurable(self) -> None:
        result = compute_safe_intervals(
            query([(100, 200)], traversal=10, uncertainty=4, buffer_us=6)
        )
        assert entries(result) == list(range(0, 81)) + list(range(210, 1_000))
        assert SafeIntervalPadding(Microsecond(4), Microsecond(6)).expansion_us == 10

    def test_padding_applies_to_both_sides_of_every_occupation(self) -> None:
        result = compute_safe_intervals(
            query([(100, 200), (400, 500)], traversal=0, uncertainty=5, buffer_us=5)
        )
        assert entries(result) == (
            list(range(0, 91)) + list(range(210, 391)) + list(range(510, 1_000))
        )

    def test_no_padding_and_no_traversal_is_the_plain_free_set(self) -> None:
        result = compute_safe_intervals(query([(100, 200)], traversal=0))
        assert [window.entry for window in result.windows] == [
            Interval.of(0, 101),
            Interval.of(200, 1_000),
        ]

    def test_padding_from_seconds_is_exact(self) -> None:
        padding = SafeIntervalPadding.from_seconds(uncertainty_s=5, transition_buffer_s=10)
        assert padding.uncertainty_us == 5_000_000
        assert padding.transition_buffer_us == 10_000_000


class TestExitEntryPair:
    def test_a_window_is_valid_only_when_exit_plus_buffer_clears(self) -> None:
        """Gap of exactly ``traversal + 2 * buffer`` admits exactly one entry."""
        result = compute_safe_intervals(
            query([(0, 100), (130, 200)], traversal=10, buffer_us=10, horizon=Interval.of(0, 200))
        )
        assert entries(result) == [110]
        window = result.windows[0]
        assert window.entry == Interval.of(110, 111)
        assert window.exit == Interval.of(120, 121)
        assert window.release == Interval.of(130, 131)
        assert window.latest_entry == 110
        assert window.traversal_us == 10

    def test_one_microsecond_short_is_not_a_window_at_all(self) -> None:
        result = compute_safe_intervals(
            query([(0, 100), (129, 200)], traversal=10, buffer_us=10, horizon=Interval.of(0, 200))
        )
        assert result.status is SafeIntervalStatus.NO_WINDOW
        assert result.windows == ()
        assert any(audit.reason is GapReason.GAP_TOO_SMALL for audit in result.gap_audit)

    def test_latest_entry_is_inclusive_never_the_exclusive_end(self) -> None:
        built = query([(100, 200)], traversal=10)
        last = compute_safe_intervals(built).windows[-1]
        assert last.entry.end == 1_000
        assert last.latest_entry == 999
        assert classify_entry(built, 999).status is EntryStatus.ADMISSIBLE
        assert classify_entry(built, 1_000).status is EntryStatus.OUTSIDE_HORIZON

    def test_a_traversal_longer_than_the_free_space_produces_nothing(self) -> None:
        result = compute_safe_intervals(query([(0, 1000)], traversal=500))
        assert result.windows == ()
        assert any(audit.reason is GapReason.GAP_TOO_SMALL for audit in result.gap_audit)

    def test_exit_may_lie_beyond_the_horizon_because_only_entry_is_bounded(self) -> None:
        """The horizon says when you may enter, not when you must arrive."""
        result = compute_safe_intervals(
            query([(0, 10)], traversal=500, horizon=Interval.of(10, 20))
        )
        assert entries(result) == list(range(10, 20))
        assert result.windows[0].exit.end == 520


class TestTypedRejections:
    def test_every_gap_is_audited_exactly_once(self) -> None:
        result = compute_safe_intervals(query([(100, 200), (400, 500)], traversal=10))
        assert len(result.gap_audit) == 3
        assert len(result.windows) + result.rejected_count == len(result.gap_audit)
        assert result.accepted_count == 3

    def test_rejection_names_both_boundning_commitments(self) -> None:
        result = compute_safe_intervals(
            query([(0, 100), (108, 200)], traversal=10, buffer_us=3)
        )
        rejected = [audit for audit in result.gap_audit if audit.reason is GapReason.GAP_TOO_SMALL]
        assert len(rejected) == 1
        assert rejected[0].bounded_before.label == "r0-100"
        assert rejected[0].bounded_after.label == "r108-200"

    def test_available_and_required_are_reported_as_numbers(self) -> None:
        result = compute_safe_intervals(
            query([(0, 100), (108, 200)], traversal=10, buffer_us=3)
        )
        audit = next(a for a in result.gap_audit if a.reason is GapReason.GAP_TOO_SMALL)
        assert audit.available_us == 3
        assert audit.required_us == 10

    def test_horizon_closed_is_distinct_from_gap_too_small(self) -> None:
        closed = compute_safe_intervals(
            query([(0, 999)], traversal=10, buffer_us=5, horizon=Interval.of(0, 100))
        )
        assert {audit.reason for audit in closed.gap_audit} == {GapReason.HORIZON_CLOSED}
        assert closed.windows == ()

    def test_blocked_windows_are_returned_for_independent_verification(self) -> None:
        result = compute_safe_intervals(query([(100, 200)], traversal=10, uncertainty=5, buffer_us=5))
        assert result.blocked == (Interval.of(81, 210),)

    def test_empty_horizon_is_a_status_not_an_exception(self) -> None:
        result = compute_safe_intervals(query([(0, 10)], horizon=Interval.of(5, 5)))
        assert result.status is SafeIntervalStatus.EMPTY_HORIZON
        assert result.windows == ()
        assert result.gap_audit == ()


class TestQueryValidation:
    def test_negative_padding_is_rejected(self) -> None:
        with pytest.raises(TimeDomainError):
            SafeIntervalPadding(Microsecond(-1), Microsecond(0))

    def test_negative_traversal_is_rejected(self) -> None:
        with pytest.raises(TimeDomainError):
            query([], traversal=-1)

    def test_empty_occupation_is_rejected(self) -> None:
        with pytest.raises(SafeIntervalError, match="empty"):
            OccupiedWindow(Interval.of(5, 5), "nothing")

    def test_duplicate_labels_are_rejected(self) -> None:
        with pytest.raises(SafeIntervalError, match="duplicate"):
            SafeIntervalQuery(
                pad_key(),
                HORIZON,
                Microsecond(10),
                ZERO_PADDING,
                (
                    OccupiedWindow(Interval.of(100, 200), "same"),
                    OccupiedWindow(Interval.of(300, 400), "same"),
                ),
            )

    def test_a_corridor_key_carrying_a_window_is_rejected(self) -> None:
        with pytest.raises(ResourceKeyError, match="no window"):
            resource_key("corridor", "C-77", window=Interval.of(0, 10))

    def test_a_bare_corridor_is_not_a_time_bounded_resource(self) -> None:
        with pytest.raises(ResourceKeyError, match="not time-bounded"):
            SafeIntervalQuery(
                resource_key("corridor", "C-77"),
                HORIZON,
                Microsecond(10),
                ZERO_PADDING,
                (),
            )

    def test_overlapping_commitments_collapse_into_one_blocked_window(self) -> None:
        result = compute_safe_intervals(query([(100, 200), (150, 260)], traversal=10))
        assert result.blocked == (Interval.of(91, 260),)
        assert result.windows[-1].bounded_before.label == "r100-200+r150-260"

    def test_touching_commitments_collapse_too(self) -> None:
        built = query([(100, 200), (200, 300)], traversal=0)
        merged = built.merged_occupied()
        assert [window.start for window in merged] == [100]
        assert merged[0].label == "r100-200+r200-300"

    def test_input_order_does_not_change_the_answer(self) -> None:
        forward = compute_safe_intervals(query([(100, 200), (400, 500), (50, 60)], traversal=9))
        backward = compute_safe_intervals(query([(50, 60), (400, 500), (100, 200)], traversal=9))
        assert forward.windows == backward.windows
        assert forward.blocked == backward.blocked
        assert forward.gap_audit == backward.gap_audit


class TestEntryClassification:
    def test_outside_horizon_is_its_own_status(self) -> None:
        built = query([(100, 200)])
        assert classify_entry(built, -1).status is EntryStatus.OUTSIDE_HORIZON
        assert classify_entry(built, 1_000).status is EntryStatus.OUTSIDE_HORIZON

    def test_blocking_reports_when_the_resource_next_frees(self) -> None:
        built = query([(100, 200)], traversal=10, uncertainty=4, buffer_us=6)
        decision = classify_entry(built, 150)
        assert decision.status is EntryStatus.BLOCKED_BY_OCCUPATION
        assert decision.blocking.window == Interval.of(100, 200)
        assert decision.earliest_next_entry == 210
        assert classify_entry(built, 210).status is EntryStatus.ADMISSIBLE

    def test_decision_carries_exit_and_release_times(self) -> None:
        decision = classify_entry(query([(500, 600)], traversal=12, buffer_us=7), 100)
        assert decision.status is EntryStatus.ADMISSIBLE
        assert decision.exit == 112
        assert decision.release == 119

    def test_occupancy_at_rejects_an_inadmissible_entry(self) -> None:
        result = compute_safe_intervals(query([(100, 200)], traversal=10))
        window = result.windows[0]
        assert window.occupancy_at(50) == Interval.of(50, 60)
        with pytest.raises(SafeIntervalError):
            window.occupancy_at(150)


class TestHelpers:
    def test_earliest_entry_on_or_after_skips_a_blocked_prefix(self) -> None:
        built = query([(100, 200)], traversal=10)
        assert earliest_entry_on_or_after(built, 0) == 0
        assert earliest_entry_on_or_after(built, 150) == 200
        assert earliest_entry_on_or_after(built, 1_000) is None

    def test_window_containing_and_earliest(self) -> None:
        result = compute_safe_intervals(query([(100, 200), (400, 500)], traversal=10))
        assert result.window_containing(550) == result.windows[-1]
        assert result.window_containing(150) is None
        assert result.earliest() is result.windows[0]

    def test_alternatives_counts_entry_instants(self) -> None:
        result = compute_safe_intervals(query([(100, 200)], traversal=10))
        assert result.windows[0].alternatives == 91

    def test_describe_result_is_reproducible_and_lists_rejections(self) -> None:
        built = query([(0, 100), (108, 200)], traversal=10, buffer_us=3)
        first = list(describe_result(compute_safe_intervals(built)))
        assert first == list(describe_result(compute_safe_intervals(built)))
        assert first[0] == "status=feasible"
        assert any(line.startswith("rejected reason=gap_too_small") for line in first)


class TestClockJumpsAndScale:
    def test_a_huge_jump_forward_still_leaves_a_window(self) -> None:
        jump = 10**15
        horizon = Interval.of(jump, jump + 1_000)
        result = compute_safe_intervals(
            query([(jump, jump + 10)], traversal=5, horizon=horizon)
        )
        assert entries(result) == list(range(jump + 10, jump + 1_000))

    def test_padding_at_the_domain_edge_saturates_instead_of_wrapping(self) -> None:
        built = SafeIntervalQuery.build(
            pad_key("EDGE"),
            horizon=Interval.of(INT64_MICROSECOND_MAX - 100, INT64_MICROSECOND_MAX),
            traversal_us=10,
            padding=SafeIntervalPadding(Microsecond(1), Microsecond(1)),
            occupied=[(Interval.of(INT64_MICROSECOND_MIN + 5, INT64_MICROSECOND_MAX - 50), "edge")],
        )
        result = compute_safe_intervals(built)
        assert result.status is SafeIntervalStatus.FEASIBLE
        # Only the entries whose release is still storable are offered.
        assert result.windows[-1].entry == Interval.of(
            INT64_MICROSECOND_MAX - 48, INT64_MICROSECOND_MAX - 11
        )
        assert result.windows[-1].release.end == INT64_MICROSECOND_MAX
        assert all(window.entry.end <= INT64_MICROSECOND_MAX for window in result.windows)

    def test_padding_at_the_low_domain_edge_saturates_too(self) -> None:
        built = SafeIntervalQuery.build(
            pad_key("EDGE2"),
            horizon=Interval.of(INT64_MICROSECOND_MIN, INT64_MICROSECOND_MIN + 100),
            traversal_us=5,
            padding=SafeIntervalPadding(Microsecond(50), Microsecond(50)),
            occupied=[(Interval.of(INT64_MICROSECOND_MIN + 40, INT64_MICROSECOND_MIN + 60), "low")],
        )
        result = compute_safe_intervals(built)
        assert result.blocked[0].start == INT64_MICROSECOND_MIN

    def test_a_full_domain_horizon_is_not_a_crash(self) -> None:
        result = compute_safe_intervals(
            query(
                [(0, 1)],
                traversal=1,
                horizon=Interval.of(INT64_MICROSECOND_MIN, INT64_MICROSECOND_MAX),
            )
        )
        assert result.windows[0].entry == Interval.of(INT64_MICROSECOND_MIN, 0)
        # The last admissible entry is the one whose release is still storable.
        assert result.windows[1].entry == Interval.of(1, INT64_MICROSECOND_MAX - 1)
        assert result.windows[1].release.end == INT64_MICROSECOND_MAX
