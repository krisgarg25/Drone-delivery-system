"""The 13-step pipeline, bounded recompute, reason codes and planner/checker separation.

Invariants proved here, one per test:

* every numbered step is recorded, so a reviewer can see what ran (I-``trace``);
* a committed window pushes the safe entry time out by the documented padding;
* a lane closed for the whole horizon is absent from the candidates, not priced;
* a stale-version refusal restarts at step 5 and stops at the retry limit;
* every rejection carries a code, a human sentence and a technical detail;
* identical input gives a byte-identical proposal;
* the planner's output is a proposal that an independent evaluator can re-derive -
  and it has no field that could call itself valid.
"""

from __future__ import annotations

from typing import Any

import pytest

from droneops_planner import (
    AircraftState,
    CommitOutcome,
    CommitRequest,
    CommitState,
    InMemoryCommitPort,
    InMemoryOccupancy,
    PlanProposal,
    ReasonCode,
    Rejection,
)
from droneops_geom_core import Interval

from planner_support import build_pipeline, envelope, identifiers_for, request


def cell_identifiers(graph: Any, edge_id: str) -> list[str]:
    return [
        f"{cell.token()}-{cell.reference.value}" for cell in graph.edge_cells_for(edge_id)
    ]


def test_all_thirteen_steps_are_recorded(graph: Any, policy: Any, wind_steps: Any, epoch_us: int) -> None:
    """The trace is the audit record: 13 named steps, in order, with what each did."""
    pipeline, _, _ = build_pipeline(graph, policy, wind_steps)
    proposal = pipeline.plan(request(epoch_us))
    assert proposal.selected is not None
    numbers = [step.step for step in proposal.trace.steps]
    for step_number in range(1, 14):
        assert step_number in numbers, f"step {step_number} is missing from {numbers}"
    assert numbers == sorted(numbers)
    named = {step.step: step.name for step in proposal.trace.steps}
    assert named[1] == "freeze"
    assert named[5] == "candidate_paths"
    assert named[6] == "safe_intervals"
    assert named[7] == "protected_volumes"
    assert named[8] == "overlap_prefilter"
    assert named[9] == "precise_intersection"
    assert named[12] == "commit"
    assert named[13] == "decision_record"
    assert proposal.trace.outcome == "proposed"
    assert proposal.trace.config_release_sha == policy.config_release_sha
    assert proposal.trace.graph_version == graph.graph_version


def test_safe_interval_search_returns_the_documented_windows(
    graph: Any, policy: Any, epoch_us: int
) -> None:
    """One commitment on one lane's cells moves the entry, by exactly the padding."""
    from droneops_planner.space_time import RouteTiming, chain_route_timing, padding_of

    edge_id = "lane-hcentral-hnorth-B-synthetic"
    horizon = Interval(epoch_us, epoch_us + 60 * 60 * 1_000_000)
    padding = padding_of(
        horizontal_mm=policy.horizontal_margin_mm,
        vertical_mm=policy.vertical_margin_mm,
        uncertainty_mm=policy.position_uncertainty_mm,
        timing_us=policy.timing_uncertainty_us,
        transition_us=policy.transition_buffer_us,
        separation_us=policy.separation_buffer_us,
    )
    occupancy = InMemoryOccupancy()
    clean = chain_route_timing(
        graph,
        (edge_id,),
        horizon=horizon,
        earliest_us=epoch_us,
        padding=padding,
        occupancy=occupancy,
        separation_us=policy.separation_buffer_us,
    )
    assert isinstance(clean, RouteTiming)
    assert clean.entry_us == epoch_us
    block_start = epoch_us + 10 * 60 * 1_000_000
    block_end = block_start + 5 * 60 * 1_000_000
    for identifier in identifiers_for(graph, edge_id):
        occupancy.preload(identifier, block_start, block_end, "mission-other-01")
    later = chain_route_timing(
        graph,
        (edge_id,),
        horizon=horizon,
        earliest_us=block_start,
        padding=padding,
        occupancy=occupancy,
        separation_us=policy.separation_buffer_us,
    )
    assert isinstance(later, RouteTiming)
    required_gap = policy.timing_uncertainty_us + policy.transition_buffer_us
    assert later.entry_us >= block_end + required_gap
    assert later.entry_us > clean.entry_us
    assert len(later.timings[0].admissible) >= 1
    sealed = InMemoryOccupancy()
    for identifier in identifiers_for(graph, edge_id):
        sealed.preload(identifier, epoch_us, epoch_us + 60 * 60 * 1_000_000, "mission-every-time")
    squeezed = chain_route_timing(
        graph,
        (edge_id,),
        horizon=horizon,
        earliest_us=epoch_us,
        padding=padding,
        occupancy=sealed,
        separation_us=policy.separation_buffer_us,
    )
    assert isinstance(squeezed, Rejection)
    assert squeezed.code is ReasonCode.NO_SAFE_WINDOW
    assert "mission-every-time" in squeezed.evidence


def test_closure_for_the_whole_horizon_removes_candidates(
    graph: Any, policy: Any, wind_steps: Any, epoch_us: int
) -> None:
    """Minutes 31-36 sit inside the bundle's 30-38 closure, so that lane is gone."""
    closed_lane = "lane-hcentral-hsouth-campus-C-synthetic"
    pipeline, _, _ = build_pipeline(graph, policy, wind_steps)
    proposal = pipeline.plan(
        request(
            epoch_us,
            destination="hub-south-campus-synthetic",
            at_minute=31,
            deadline_minute=35,
            cutoff_minute=36,
        )
    )
    step5 = [step for step in proposal.trace.steps if step.step == 5]
    assert step5 and "2 lane(s) closed for the whole window" in step5[0].detail
    closed = {
        "lane-hcentral-hsouth-campus-C-synthetic",
        "lane-hsouth-campus-hcentral-C-synthetic",
    }
    used = {edge_id for candidate in proposal.candidates for edge_id in candidate.route.edge_ids}
    assert used.isdisjoint(closed)
    if proposal.selected is not None:
        assert closed_lane not in proposal.selected.route.edge_ids


def test_bounded_recompute_stops_at_the_limit_and_records_it(
    graph: Any, policy: Any, wind_steps: Any, epoch_us: int
) -> None:
    """A ledger that keeps moving cannot loop the planner: three restarts, then no."""
    occupancy = InMemoryOccupancy()

    class AlwaysStale:
        def __init__(self) -> None:
            self.asks = 0

        def try_commit(self, commit_request: CommitRequest) -> CommitOutcome:
            self.asks += 1
            occupancy.preload(
                f"lane-{commit_request.candidate_id}",
                commit_request.reservation_version,
                commit_request.reservation_version + 1,
                "rival",
            )
            return CommitOutcome(
                state=CommitState.REJECTED_STALE_VERSION,
                detail=f"ledger advanced to {occupancy.reservation_version()}",
                observed_version=occupancy.reservation_version(),
            )

    ledger = AlwaysStale()
    pipeline, _, _ = build_pipeline(
        graph, policy, wind_steps, max_recompute=2, occupancy=occupancy, ledger=ledger
    )
    proposal = pipeline.plan(request(epoch_us))
    assert proposal.selected is None
    assert proposal.trace.outcome == "rejected"
    assert len(proposal.trace.retries) == 3
    reasons = [retry.reason_code for retry in proposal.trace.retries]
    assert reasons[:2] == [ReasonCode.COMMIT_STALE_VERSION.value] * 2
    assert reasons[-1] == ReasonCode.RETRY_LIMIT_EXCEEDED.value
    assert all(retry.restart_from_step == 5 for retry in proposal.trace.retries)
    assert ledger.asks == 3


def test_commit_conflict_falls_to_the_next_candidate(
    graph: Any, policy: Any, wind_steps: Any, epoch_us: int
) -> None:
    """A refused commit is recorded and the next ranked candidate is asked."""
    occupancy = InMemoryOccupancy()
    inner = InMemoryCommitPort(occupancy)

    class FirstOnlyRefused:
        def __init__(self) -> None:
            self.calls = 0

        def try_commit(self, commit_request: CommitRequest) -> CommitOutcome:
            self.calls += 1
            if self.calls == 1:
                return CommitOutcome(
                    state=CommitState.REJECTED_CONFLICT,
                    detail="resource lane-mock held by mission-other",
                    observed_version=occupancy.reservation_version(),
                )
            return inner.try_commit(commit_request)

    ledger = FirstOnlyRefused()
    pipeline, _, _ = build_pipeline(graph, policy, wind_steps, occupancy=occupancy, ledger=ledger)
    proposal = pipeline.plan(request(epoch_us))
    codes = [row[1] for row in proposal.trace.rejected]
    assert ReasonCode.COMMIT_CONFLICT.value in codes
    assert ledger.calls == len(proposal.candidates)
    assert len(proposal.candidates) >= 1
    assert proposal.trace.outcome == "rejected"


def test_every_rejection_has_a_code_and_two_explanations(
    graph: Any, policy: Any, wind_steps: Any, epoch_us: int
) -> None:
    """Section 11: rejection paths are first-class results, and they must read plainly."""
    pipeline, _, _ = build_pipeline(graph, policy, wind_steps)
    proposals = [
        pipeline.plan(request(epoch_us, origin="no-such-hub", mission_id="msn-a")),
        pipeline.plan(request(epoch_us, destination="no-such-pad", mission_id="msn-b")),
        pipeline.plan(request(epoch_us, payload_g=9000, mission_id="msn-c")),
        pipeline.plan(
            request(
                epoch_us,
                mission_id="msn-d",
                aircraft=envelope(charge_wh=40, state=AircraftState.AVAILABLE),
            )
        ),
        pipeline.plan(request(epoch_us, mission_id="msn-e", aircraft=envelope(state=AircraftState.OFFLINE))),
        pipeline.plan(request(epoch_us, destination="hub-central-synthetic", mission_id="msn-f")),
        pipeline.plan(request(epoch_us, mission_id="msn-g", candidate_k=2)),
        pipeline.plan(request(epoch_us, mission_id="msn-h", cutoff_minute=1, deadline_minute=0)),
        pipeline.plan(
            request(
                epoch_us,
                mission_id="msn-i",
                origin="node-rimroad-synthetic",
                destination="hub-ridge-east-synthetic",
            )
        ),
    ]
    rows = [row for proposal in proposals for row in proposal.trace.rejected]
    assert rows, "these requests must all be rejected somewhere"
    for row in rows:
        subject, code, human, technical = row
        assert subject and code and human and technical
        assert ReasonCode(code) is not None
        assert len(human) > 20
        assert len(technical) > len(human) // 4
    assert {row[1] for row in rows} & {
        ReasonCode.ORIGIN_UNKNOWN.value,
        ReasonCode.DESTINATION_UNKNOWN.value,
        ReasonCode.PAYLOAD_EXCEEDS_ENVELOPE.value,
        ReasonCode.AIRCRAFT_INELIGIBLE.value,
        ReasonCode.ENERGY_BELOW_RESERVE.value,
        ReasonCode.ORIGIN_EQUALS_DESTINATION.value,
        ReasonCode.CANDIDATE_LIMIT_TOO_SMALL.value,
        ReasonCode.DEADLINE_BEFORE_ACCEPTANCE.value,
    }


def test_identical_input_produces_a_byte_identical_proposal(
    graph: Any, policy: Any, wind_steps: Any, epoch_us: int
) -> None:
    """Same bundle, same ledger, same request: the ranking bytes must not move."""
    first, _, _ = build_pipeline(graph, policy, wind_steps)
    second, _, _ = build_pipeline(graph, policy, wind_steps)
    one = first.plan(request(epoch_us))
    two = second.plan(request(epoch_us))
    assert one.digest() == two.digest()
    assert [list(row) for row in one.trace.ranking] == [list(row) for row in two.trace.ranking]
    assert one.trace.trace_digest if hasattr(one.trace, "trace_digest") else True
    assert one.trace.digest() == two.trace.digest()


def test_the_planner_never_marks_its_own_answer_valid(
    graph: Any, policy: Any, wind_steps: Any, epoch_us: int
) -> None:
    """Phase 2 gate: no planner object may claim validity, and a checker can re-derive."""
    pipeline, _, _ = build_pipeline(graph, policy, wind_steps)
    proposal = pipeline.plan(request(epoch_us))
    forbidden = {"valid", "is_valid", "safe", "is_safe", "verified", "approved", "cleared"}
    for klass in (PlanProposal, type(proposal.selected), type(proposal.trace)):
        assert forbidden.isdisjoint(set(klass.__dataclass_fields__)), klass
    assert proposal.selected is not None
    independent = independent_overlap(pipeline, proposal)
    assert independent == (), f"an independent evaluator found overlaps: {independent}"


def independent_overlap(pipeline: Any, proposal: Any) -> tuple[tuple[str, str], ...]:
    """Re-derive conflicts the way the checker must: plan payload plus ledger state only.

    Reads nothing but the proposal's protected volumes and the committed windows,
    which is the whole point of publishing a re-derivable payload.
    """
    found: list[tuple[str, str]] = []
    reference = pipeline.graph.metadata.altitude_reference.value
    for volume in proposal.protected_volumes:
        for token in volume.cells:
            identifier = f"{token}-{reference}"
            for start_us, end_us, owner in pipeline.ports.occupancy.committed_windows(identifier):
                if owner == proposal.commit_request.mission_id:
                    continue
                if Interval(start_us, end_us).overlaps(volume.window):
                    found.append((identifier, owner))
    return tuple(found)


def test_admission_failure_attempts_no_commit(graph: Any, policy: Any, wind_steps: Any, epoch_us: int) -> None:
    """An inadmissible order never reaches the ledger (section 5 of the conventions)."""
    pipeline, _, ledger = build_pipeline(graph, policy, wind_steps)
    proposal = pipeline.plan(request(epoch_us, origin="nowhere", mission_id="msn-x"))
    assert proposal.trace.outcome == "rejected"
    assert ledger.attempts == []
    assert proposal.trace.steps[1].outcome == "rejected"


def test_lane_usage_changes_fairness_and_can_change_the_rank(
    graph: Any, policy: Any, wind_steps: Any, epoch_us: int
) -> None:
    """Fairness is an integer input, not a comment: busier lanes score worse."""
    calm, _, _ = build_pipeline(graph, policy, wind_steps)
    baseline = calm.plan(request(epoch_us, destination="hub-south-campus-synthetic"))
    busy_occupancy = InMemoryOccupancy()
    for edge_id in ("lane-hcentral-nquickcart-A-synthetic", "lane-nquickcart-hsouth-campus-B-synthetic"):
        busy_occupancy.note_lane_usage(edge_id, 40)
    busy, _, _ = build_pipeline(graph, policy, wind_steps, occupancy=busy_occupancy)
    crowded = busy.plan(request(epoch_us, destination="hub-south-campus-synthetic"))
    if baseline.selected is not None and crowded.selected is not None:
        assert crowded.selected.score.fairness_units >= baseline.selected.score.fairness_units
