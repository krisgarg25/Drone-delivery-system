"""The geom-4d seam and the payload a checker needs to re-derive the answer itself.

Two things are proved here and nowhere else:

* the pipeline runs end to end with ``droneops_geom_4d`` building the protected
  volumes, so the swept-volume kernel is a used dependency rather than a declared
  one (the planner must not reimplement it);
* the proposal carries everything an independent evaluator needs - raw 4D profile
  points, canonical resource keys, the claimed windows and the config release hash -
  and still carries no verdict of its own.
"""

from __future__ import annotations

from typing import Any

from droneops_geom_4d import ProtectedBody
from droneops_geom_core import RESOURCE_KIND_ORDER

kind_order = [
    item.value if hasattr(item, "value") else str(item) for item in RESOURCE_KIND_ORDER
]
from droneops_planner import (
    EdgeTiming,
    Geom4dSweepVolumeBuilder,
    InMemoryOccupancy,
    lane_profile,
    policy_from_snapshot,
    route_profile,
)

from planner_support import build_pipeline, identifiers_for, request

TEST_AIRFRAME = ProtectedBody.of(0.75, 0.25, "synthetic-test-airframe")
"""A stated test dimension: the body is an input to the kernel, never a planner default."""


def geom4d_builder(graph: Any, policy: Any) -> Geom4dSweepVolumeBuilder:
    kernel_policy = policy_from_snapshot(
        graph,
        policy_version=policy.policy_version,
        horizontal_margin_mm=policy.horizontal_margin_mm,
        vertical_margin_mm=policy.vertical_margin_mm,
        position_uncertainty_mm=policy.position_uncertainty_mm,
        timing_uncertainty_us=policy.timing_uncertainty_us,
        transition_buffer_us=policy.transition_buffer_us,
    )
    return Geom4dSweepVolumeBuilder(graph, kernel_policy, TEST_AIRFRAME)


def test_the_pipeline_runs_on_real_swept_volumes(
    graph: Any, policy: Any, wind_steps: Any, epoch_us: int
) -> None:
    """Steps 7 and 8 through geom-4d: continuous prisms rasterised onto cells."""
    builder = geom4d_builder(graph, policy)
    pipeline, _, _ = build_pipeline(graph, policy, wind_steps, volumes=builder)
    proposal = pipeline.plan(request(epoch_us, mission_id="msn-geom4d"))
    assert proposal.selected is not None, [row[1] for row in proposal.trace.rejected]
    volumes = proposal.protected_volumes
    assert volumes, "a swept route protects volume on every leg"
    assert all(volume.cells for volume in volumes)
    assert volumes == tuple(
        sorted(volumes, key=lambda item: (item.window.start, item.edge_id))
    ), "published volumes are in the documented order"
    starts = [volume.window.start for volume in volumes]
    assert starts == sorted(starts)
    for volume in volumes:
        assert volume.window.start < volume.window.end
        assert volume.cells
    assert volumes[0].window.start <= proposal.selected.entry_us
    assert builder.index().volumes, "the step 8 pre-filter index is buildable from the sweep"


def test_one_prism_per_leg_not_one_per_frame(
    graph: Any, policy: Any, wind_steps: Any, epoch_us: int
) -> None:
    """The sweep is continuous: a leg is one volume, whatever the frame rate says.

    This is the property ``technical-build/11`` section 4 buys - a per-frame sampler
    reports clearance at frame *n* and frame *n+1* and can still miss the contact
    between them, so the planner publishes leg volumes rather than snapshots.
    """
    builder = geom4d_builder(graph, policy)
    pipeline, _, _ = build_pipeline(graph, policy, wind_steps, volumes=builder)
    proposal = pipeline.plan(request(epoch_us, mission_id="msn-cover"))
    assert proposal.selected is not None
    centreline_segments = sum(
        len(graph.edge(edge_id).geometry) - 1 for edge_id in proposal.selected.route.edge_ids
    )
    swept = [
        volume
        for volume in proposal.protected_volumes
        if not volume.edge_id.startswith("approach-")
    ]
    assert len(swept) == centreline_segments, "one swept prism per declared centreline segment"
    assert swept[0].window.start <= proposal.selected.entry_us
    assert swept[-1].window.end >= proposal.selected.exit_us
    assert all(volume.cells for volume in swept)


def test_proposal_exposes_the_rederivable_payload(
    graph: Any, policy: Any, wind_steps: Any, epoch_us: int
) -> None:
    """Raw points, resource keys, windows and the config hash: all on the proposal."""
    pipeline, _, _ = build_pipeline(graph, policy, wind_steps)
    proposal = pipeline.plan(request(epoch_us, mission_id="msn-payload"))
    assert proposal.selected is not None
    points = proposal.raw_profile_points
    assert len(points) >= 2 * len(proposal.selected.route.edge_ids)
    assert [point.at_us for point in points] == sorted(point.at_us for point in points)
    assert all(
        point.at_us < following.at_us
        for point, following in zip(points, points[1:], strict=False)
    ), "a profile that revisits an instant is not a function of time"
    assert {point.altitude.reference.value for point in points} == {"AGL"}
    assert all(point.speed_mmps > 0 for point in points)
    keys = list(proposal.resource_keys)
    assert proposal.resource_keys == proposal.selected.lock_order
    assert len(set(keys)) == len(keys), "a plan cannot claim one resource twice"
    ranks = [kind_order.index(key.split(":")[0]) for key in keys]
    assert ranks == sorted(ranks), "keys are in the canonical geom-core lock order"
    assert proposal.config_release_sha == policy.config_release_sha
    assert proposal.occupancy_windows == proposal.resources
    assert all(start < end for _identifier, start, end in proposal.resources)
    assert not {"valid", "safe", "verified", "approved"} & set(
        type(proposal).__dataclass_fields__
    )


def test_the_same_inputs_sweep_the_same_volumes(
    graph: Any, policy: Any, wind_steps: Any, epoch_us: int
) -> None:
    """geom-4d adds no nondeterminism: same bundle, same ledger, same proposal digest."""
    first, _, _ = build_pipeline(
        graph, policy, wind_steps, volumes=geom4d_builder(graph, policy),
        occupancy=InMemoryOccupancy(),
    )
    second, _, _ = build_pipeline(
        graph, policy, wind_steps, volumes=geom4d_builder(graph, policy),
        occupancy=InMemoryOccupancy(),
    )
    one = first.plan(request(epoch_us, mission_id="msn-sweep-a"))
    two = second.plan(request(epoch_us, mission_id="msn-sweep-a"))
    assert [volume.digest() for volume in one.protected_volumes] == [
        volume.digest() for volume in two.protected_volumes
    ]
    assert one.digest() == two.digest()


def test_a_lane_profile_is_built_from_declared_centreline_geometry(
    graph: Any, policy: Any
) -> None:
    """The published points are the bundle's own vertices, not a chord between ends."""
    edge_id = "lane-hcentral-hnorth-B-synthetic"
    edge = graph.edge(edge_id)
    epoch_us = graph.metadata.epoch_us
    profile = lane_profile(
        graph,
        edge,
        entry_us=epoch_us,
        motion_end_us=epoch_us + edge.cruise_time_us(),
        plan_version=policy.policy_version,
        source=graph.metadata.source,
    )
    assert len(profile.points) == len(edge.geometry)
    assert profile.points[0].at_us == epoch_us
    assert profile.points[-1].at_us == epoch_us + edge.cruise_time_us()
    assert profile.window.end > profile.window.start
    whole = route_profile(
        graph,
        (edge_id,),
        (
            EdgeTiming(
                edge_id=edge_id,
                entry_us=epoch_us,
                exit_us=epoch_us + edge.cruise_time_us(),
                release_us=epoch_us + edge.cruise_time_us() + 1,
                admissible=(),
                blocked_by=(),
            ),
        ),
        plan_version=policy.policy_version,
        source=graph.metadata.source,
    )
    assert len(whole.points) == len(edge.geometry)


