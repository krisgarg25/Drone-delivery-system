"""Candidate generation: k-shortest order, tie-breaks, closures and mid-plan expiry.

Invariant under test: the ranked candidate list is a function of bundle content and
the cost profile only. Equal-cost routes are separated by node id then lane id, and
a closed lane is *absent*, not expensive.
"""

from __future__ import annotations

from typing import Any

import pytest

from droneops_corridor_graph import (
    CandidateSet,
    ClosureOverlay,
    CostProfile,
    PathGenerationError,
    Restriction,
    RestrictionKind,
    generate_candidates,
    minutes_to_us,
)
from droneops_geom_core import Interval

from corridor_support import build_square_graph, closure, overlay_with

START = "source"
GOAL = "target"


@pytest.fixture
def square() -> Any:
    return build_square_graph()


def route_ids(candidates: CandidateSet) -> list[tuple[str, ...]]:
    return [path.edge_ids for path in candidates.candidates]


def test_two_equal_cost_detours_break_ties_on_node_then_lane(square: Any) -> None:
    """The documented total order: cost, then the lane sequence, never insertion order."""
    candidates = generate_candidates(
        square, START, GOAL, k=2, profile=CostProfile(), window=Interval(0, 10**9)
    )
    assert len(candidates.candidates) == 2
    assert candidates.candidates[0].cost_units == candidates.candidates[1].cost_units
    assert route_ids(candidates) == [
        ("lane-s-mid-a", "lane-mid-a-t"),
        ("lane-s-mid-b", "lane-mid-b-t"),
    ]
    assert candidates.candidates[0].node_ids[1] == "mid-a"


def test_candidates_are_loopless_and_ordered_by_cost(square: Any) -> None:
    candidates = generate_candidates(
        square, START, GOAL, k=5, profile=CostProfile(merge_penalty_units=3),
        window=Interval(0, 10**9),
    )
    assert len(candidates.candidates) == 2
    assert candidates.exhausted is True
    assert candidates.requested_k == 5
    for path in candidates.candidates:
        assert len(set(path.node_ids)) == len(path.node_ids)
        assert path.node_ids[0] == START and path.node_ids[-1] == GOAL
    costs = [path.cost_units for path in candidates.candidates]
    assert costs == sorted(costs)


def test_closure_of_one_lane_removes_every_candidate_that_uses_it(square: Any) -> None:
    """A lane closed for the whole plan window is not in the graph at all."""
    blocked = closure("lane-s-mid-a", 0, 60, epoch_us=0)
    graph = build_square_graph(overlay=overlay_with(blocked))
    candidates = generate_candidates(
        graph, START, GOAL, k=4, profile=CostProfile(), window=Interval(0, 60 * 60_000_000)
    )
    assert route_ids(candidates) == [("lane-s-mid-b", "lane-mid-b-t")]
    assert candidates.closed_lane_ids == ("lane-s-mid-a",)


def test_closures_on_both_detours_leave_no_candidate(square: Any) -> None:
    graph = build_square_graph(
        overlay=overlay_with(
            closure("lane-s-mid-a", 0, 60, epoch_us=0),
            closure("lane-s-mid-b", 0, 60, epoch_us=0),
        )
    )
    with pytest.raises(PathGenerationError, match="closures removed every route"):
        generate_candidates(
            graph, START, GOAL, k=3, profile=CostProfile(), window=Interval(0, 60 * 60_000_000)
        )


def test_restriction_expiring_mid_plan_is_representable(square: Any) -> None:
    """Minutes 30-38 closed leaves two usable sub-windows, so a plan may still fly.

    This is the case a static open/closed graph cannot express: the lane is neither
    usable nor unusable for the whole 60 minute horizon.
    """
    graph = build_square_graph(
        overlay=overlay_with(closure("lane-s-mid-a", 30, 38, epoch_us=0))
    )
    horizon = Interval(0, 60 * 60_000_000)
    view = graph.view_under_overlay(horizon.start, horizon.end)
    assert view.fully_closed() == ()
    available = view.availability("lane-s-mid-a")
    assert len(available) == 2
    assert available[0].end == minutes_to_us(30, 0)
    assert available[1].start == minutes_to_us(38, 0)
    assert [item.restriction_id for item in view.graph.overlay.mid_plan_expiries(horizon)] == [
        "closure-lane-s-mid-a-30-38"
    ]
    candidates = generate_candidates(graph, START, GOAL, k=2, profile=CostProfile(), window=horizon)
    assert ("lane-s-mid-a", "lane-mid-a-t") in route_ids(candidates)


def test_bundle_closure_overlay_is_time_absolute(synthetic_graph: Any) -> None:
    """The real bundle closes the C lane for minutes 30-38 of its own epoch."""
    epoch = synthetic_graph.metadata.epoch_us
    closed = [item.edge_id for item in synthetic_graph.overlay.restrictions]
    assert closed == [
        "lane-hcentral-hsouth-campus-C-synthetic",
        "lane-hsouth-campus-hcentral-C-synthetic",
    ]
    for item in synthetic_graph.overlay.restrictions:
        assert item.start_us == minutes_to_us(30, epoch)
        assert item.end_us == minutes_to_us(38, epoch)
        assert item.human_reason == "synthetic crane survey overlay"
    horizon = Interval(epoch, minutes_to_us(60, epoch))
    view = synthetic_graph.view_under_overlay(horizon.start, horizon.end)
    assert view.fully_closed() == ()
    assert len(view.availability(closed[0])) == 2


def test_cost_profile_prices_band_change_merge_and_crossing(square: Any) -> None:
    """A profile that prefers band B costs a band-C detour more, in integers."""
    plain = generate_candidates(
        square, START, GOAL, k=2, profile=CostProfile(), window=Interval(0, 10**9)
    )
    picky = generate_candidates(
        square,
        START,
        GOAL,
        k=2,
        profile=CostProfile(target_band="C", off_target_band_units=10_000),
        window=Interval(0, 10**9),
    )
    assert plain.candidates[0].cost_units < picky.candidates[0].cost_units
    assert plain.candidates[0].edge_ids == picky.candidates[0].edge_ids
    band_c = build_square_graph(east_band="C")
    priced = generate_candidates(
        band_c, START, GOAL, k=1, profile=CostProfile(target_band="A", off_target_band_units=7),
        window=Interval(0, 10**9),
    )
    assert priced.candidates[0].cost_units == 2 * (4000 // 1000 + 7)


def test_unknown_endpoints_and_degenerate_requests_are_rejected(square: Any) -> None:
    with pytest.raises(PathGenerationError, match="k must be at least"):
        generate_candidates(square, START, GOAL, k=0, profile=CostProfile())
    with pytest.raises(Exception, match="unknown node"):
        generate_candidates(square, "nowhere", GOAL, k=2, profile=CostProfile())
    with pytest.raises(PathGenerationError, match="origin and destination"):
        generate_candidates(square, START, START, k=2, profile=CostProfile())


def test_overlay_rejects_a_lane_the_bundle_does_not_declare(square: Any) -> None:
    from droneops_corridor_graph import ClosureOverlayError
    from droneops_corridor_graph.overlay import overlay_from_document

    with pytest.raises(ClosureOverlayError, match="does not declare"):
        overlay_from_document(
            {"closures": [{"corridor_id": "lane-ghost", "from_minute": 0, "to_minute": 5}]},
            epoch_us=0,
            known_edges=tuple(sorted(square.edges)),
        )
    with pytest.raises(ClosureOverlayError, match="ends at"):
        Restriction(
            restriction_id="bad",
            edge_id="lane-s-mid-a",
            kind=RestrictionKind.CLOSED,
            start_us=10,
            end_us=5,
            human_reason="reversed window",
            technical_detail="n/a",
        )
    assert ClosureOverlay.empty().restrictions == ()
