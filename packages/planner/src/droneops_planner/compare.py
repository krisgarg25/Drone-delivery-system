"""Fair three-way strategy comparison over identical demand.

``docs/05-chandigarh-demo.md`` section 5.6 lists what must be held constant: demand,
weather, aircraft states, seed, completion cutoff, energy profiles, pad and crew
capacity. :class:`ComparisonInputs` makes that structural rather than a promise -
all three strategies are built from the one frozen input object, each with its own
clean copy of the ledger, so a committed plan in one run cannot shadow another.

The metrics count rejected and unfinished work, not only successes
(``technical-build/11`` section 9): every rejection is tallied by reason code.
"""

from __future__ import annotations

from dataclasses import dataclass

from droneops_corridor_graph import CorridorGraph

from .defaults import (
    BatteryCurveEnergyModel,
    BundleFacilityScheduler,
    CellSweepVolumeBuilder,
    FrozenWindTrace,
    InMemoryCommitPort,
    InMemoryOccupancy,
)
from .model_util import canonical_digest
from .pipeline import CandidatePlanPipeline, PipelineConfig, PipelinePorts
from .reasons import ReasonCode
from .strategies import StrategyRegistry
from .types import PlanProposal, PlanRequest, PolicySnapshot


@dataclass(frozen=True)
class ComparisonInputs:
    """Everything two strategy runs must agree on, in one frozen object."""

    graph: CorridorGraph
    policy: PolicySnapshot
    requests: tuple[PlanRequest, ...]
    wind_steps: tuple[tuple[int, int, int], ...]
    committed: tuple[tuple[str, int, int, str], ...]
    lane_usage: tuple[tuple[str, int], ...] = ()
    seed: int = 0
    cutoff_us: int = 0

    def digest(self) -> str:
        """Shared-input identity: equal digests mean the comparison was fair."""
        return canonical_digest(
            [
                self.graph.topology_digest(),
                self.policy.digest(),
                [request.request_digest() for request in self.requests],
                list(self.wind_steps),
                [list(item) for item in self.committed],
                list(self.lane_usage),
                self.seed,
                self.cutoff_us,
            ]
        )


@dataclass(frozen=True)
class StrategyMetrics:
    """What one strategy did with the shared demand."""

    strategy: str
    accepted: int
    rejected: int
    rejection_counts: tuple[tuple[str, int], ...]
    total_energy_wh: int
    total_lateness_us: int
    mean_lateness_us: int
    total_crossings: int
    total_pad_wait_us: int
    recompute_attempts: int
    proposal_digests: tuple[str, ...]

    def as_row(self) -> tuple[str, int, int, int, int]:
        return (self.strategy, self.accepted, self.rejected, self.total_energy_wh, self.mean_lateness_us)


class StrategyComparator:
    """Run the same demand through each strategy and report comparable numbers."""

    def __init__(self, inputs: ComparisonInputs) -> None:
        self.inputs = inputs

    def run(self, strategies: tuple[str, ...]) -> tuple[StrategyMetrics, ...]:
        """One isolated pipeline per strategy; nothing is shared but the inputs."""
        return tuple(self._run_one(name) for name in sorted(strategies))

    def proposals(self, strategy: str) -> tuple[PlanProposal, ...]:
        return tuple(self._pipeline(strategy, fresh_ledger=True).plan(request) for request in self.inputs.requests)

    def _run_one(self, strategy: str) -> StrategyMetrics:
        pipeline = self._pipeline(strategy, fresh_ledger=True)
        accepted = 0
        counts: dict[str, int] = {}
        energy = 0
        lateness = 0
        crossings = 0
        pad_wait = 0
        recompute = 0
        digests: list[str] = []
        for request in self.inputs.requests:
            proposal = pipeline.plan(request)
            digests.append(proposal.digest())
            for row in proposal.trace.rejected:
                counts[row[1]] = counts.get(row[1], 0) + 1
            recompute += len(proposal.trace.retries)
            selected = proposal.selected
            if selected is None:
                rejected_codes = {row[1] for row in proposal.trace.rejected}
                if not rejected_codes:
                    counts[ReasonCode.UNPROVEN_DEPENDENCY.value] = (
                        counts.get(ReasonCode.UNPROVEN_DEPENDENCY.value, 0) + 1
                    )
                continue
            accepted += 1
            energy += selected.energy.consumption_wh
            lateness += selected.score.lateness_us
            crossings += selected.route.crossing_count
            pad_wait += selected.score.pad_wait_us
        rejected = sum(counts.values())
        return StrategyMetrics(
            strategy=strategy,
            accepted=accepted,
            rejected=rejected,
            rejection_counts=tuple(sorted(counts.items())),
            total_energy_wh=energy,
            total_lateness_us=lateness,
            mean_lateness_us=lateness // accepted if accepted else 0,
            total_crossings=crossings,
            total_pad_wait_us=pad_wait,
            recompute_attempts=recompute,
            proposal_digests=tuple(digests),
        )

    def _pipeline(self, strategy: str, *, fresh_ledger: bool) -> CandidatePlanPipeline:
        del fresh_ledger
        occupancy = InMemoryOccupancy()
        for identifier, start_us, end_us, owner in self.inputs.committed:
            occupancy.preload(identifier, start_us, end_us, owner)
        for edge_id, count in self.inputs.lane_usage:
            occupancy.note_lane_usage(edge_id, count)
        wind = FrozenWindTrace(self.inputs.wind_steps, self.inputs.graph.metadata.epoch_us)
        ports = PipelinePorts(
            occupancy=occupancy,
            volumes=CellSweepVolumeBuilder(self.inputs.graph),
            commit=InMemoryCommitPort(occupancy),
            energy=BatteryCurveEnergyModel(),
            facilities=BundleFacilityScheduler(self.inputs.graph, occupancy),
            wind=wind,
        )
        return CandidatePlanPipeline(
            self.inputs.graph,
            self.inputs.policy,
            ports,
            PipelineConfig(strategy=strategy),
            registry=StrategyRegistry.default(self.inputs.graph),
        )


def identical_inputs(
    graph: CorridorGraph,
    policy: PolicySnapshot,
    requests: tuple[PlanRequest, ...],
    wind_steps: tuple[tuple[int, int, int], ...],
    committed: tuple[tuple[str, int, int, str], ...] = (),
    *,
    seed: int = 0,
) -> ComparisonInputs:
    """Build the shared comparison input once, with the cutoff the bundle declares."""
    cutoff = max(request.cutoff_us for request in requests) if requests else 0
    return ComparisonInputs(
        graph=graph,
        policy=policy,
        requests=tuple(sorted(requests, key=lambda item: (item.requested_at_us, item.order_id))),
        wind_steps=wind_steps,
        committed=committed,
        seed=seed,
        cutoff_us=cutoff,
    )


__all__ = [
    "ComparisonInputs",
    "StrategyComparator",
    "StrategyMetrics",
    "identical_inputs",
]
