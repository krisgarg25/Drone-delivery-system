"""Golden facility scenarios, in the shape ``technical-build/06`` section 6 demands.

Every safety-critical scenario carries five fields, and this type cannot be
built without them: a human description, a machine-readable input, the expected
invariant, the expected metrics, and **what this does not prove**. The last one
is not a disclaimer field - a scenario that passes while proving less than its
name suggests is how a saturated hub gets mistaken for a validated one.

The runner compares a metric vector, not a transcript: a scenario whose
scheduling order changed without changing the outcome is a determinism problem
for another layer to catch, and a scenario whose numbers changed is a behaviour
change.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .audit import CapacityAudit
from .errors import FacilitiesError
from .model import CapacityProfile, OutageWindow, ResourceClaim, TurnaroundRequest
from .occupancy import AppendOnlyLedger
from .queueing import FacilityQueue
from .scheduler import FacilityScheduler, profile_in_outage

I6_INVARIANT = "max(overlap_count(commitments)) <= capacity"


class ScenarioError(FacilitiesError):
    """A golden scenario is incomplete or its metrics drifted."""


@dataclass(frozen=True)
class ScenarioInput:
    """The machine-readable input: sites, arrivals, and what was already held."""

    profiles: tuple[CapacityProfile, ...]
    requests: tuple[TurnaroundRequest, ...]
    committed: tuple[ResourceClaim, ...] = ()
    outages: tuple[tuple[str, OutageWindow], ...] = ()


@dataclass(frozen=True)
class GoldenScenario:
    """One scenario, with the five fields section 6 requires."""

    name: str
    description: str
    input: ScenarioInput
    expected_invariant: str
    expected_metrics: Mapping[str, int]
    does_not_prove: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.name or not self.description:
            raise ScenarioError(f"scenario {self.name!r} needs a name and a human description")
        if not self.expected_invariant:
            raise ScenarioError(f"scenario {self.name} must name the invariant it exercises")
        if not self.does_not_prove:
            raise ScenarioError(
                f"scenario {self.name} must say what it does not prove (section 6 requirement)"
            )
        if not self.expected_metrics:
            raise ScenarioError(f"scenario {self.name} must carry expected metrics")


@dataclass(frozen=True)
class GoldenRun:
    """A replay of a scenario against its expectations."""

    scenario_name: str
    metrics: dict[str, int]
    audits: tuple[CapacityAudit, ...]
    differing: tuple[tuple[str, int, int], ...]

    @property
    def passed(self) -> bool:
        return not self.differing

    def summary(self) -> str:
        lines = [f"scenario {self.scenario_name}: {'PASS' if self.passed else 'FAIL'}"]
        for key in sorted(self.metrics):
            lines.append(f"  {key}={self.metrics[key]}")
        for key, expected, actual in self.differing:
            lines.append(f"  differ {key}: expected {expected} got {actual}")
        return "\n".join(lines)


def apply_outages(
    profiles: tuple[CapacityProfile, ...], outages: tuple[tuple[str, OutageWindow], ...]
) -> tuple[CapacityProfile, ...]:
    """Return new profiles carrying the declared outages; the inputs are intact."""
    if not outages:
        return profiles
    by_id = {profile.facility_id: profile for profile in profiles}
    out: list[CapacityProfile] = []
    for profile in profiles:
        for facility_id, outage in outages:
            if facility_id == profile.facility_id:
                by_id[facility_id] = profile_in_outage(by_id[facility_id], outage)
        out.append(by_id[profile.facility_id])
    return tuple(out)


def run_golden(scenario: GoldenScenario, *, queue: FacilityQueue | None = None) -> GoldenRun:
    """Replay a scenario and compare its metric vector.

    Raises:
        ScenarioError: the scenario names a facility that is not in its input.
    """
    known = {profile.facility_id for profile in scenario.input.profiles}
    for request in scenario.input.requests:
        if request.facility_id not in known:
            raise ScenarioError(f"{scenario.name}: request {request.request_id} targets unknown site "
                                f"{request.facility_id!r}")
    profiles = apply_outages(scenario.input.profiles, scenario.input.outages)
    ledger = AppendOnlyLedger(scenario.input.committed)
    scheduler = FacilityScheduler(profiles, committed=ledger, queue=queue)
    outcome = scheduler.schedule(scenario.input.requests)

    metrics: dict[str, int] = {
        "scheduled": len(outcome.scheduled),
        "queued": len(outcome.queued),
        "rejected": len(outcome.rejected),
        "requests": len(scenario.input.requests),
        "committed_before": len(scenario.input.committed),
        "i6_holds": int(outcome.i6_holds),
        "queue_growth": len(outcome.queued),
        "wait_total_us": 0,
    }
    for audit in outcome.audits:
        metrics[f"peak_pad_overlap::{audit.facility_id}"] = audit.pads.peak_overlap
        metrics[f"pad_capacity::{audit.facility_id}"] = audit.pads.capacity
        metrics[f"peak_charger_overlap::{audit.facility_id}"] = audit.chargers.peak_overlap
        metrics[f"charger_capacity::{audit.facility_id}"] = audit.chargers.capacity
        metrics[f"peak_crew_overlap::{audit.facility_id}"] = audit.crew.peak_overlap
        metrics["i6_holds"] = int(metrics["i6_holds"] and audit.i6_holds)
    for key in ("pad_wait_us", "charger_wait_us", "crew_wait_us", "peak_queue", "total_wait_us"):
        raw = outcome.metrics.get(key, 0)
        metrics[key] = raw if isinstance(raw, int) else 0
    metrics["wait_total_us"] = (
        metrics["pad_wait_us"] + metrics["charger_wait_us"] + metrics["crew_wait_us"]
    )

    differing = tuple(
        sorted(
            (key, expected, metrics.get(key, -1))
            for key, expected in scenario.expected_metrics.items()
            if metrics.get(key) != expected
        )
    )
    return GoldenRun(
        scenario_name=scenario.name,
        metrics=dict(sorted(metrics.items())),
        audits=outcome.audits,
        differing=differing,
    )


__all__ = [
    "I6_INVARIANT",
    "GoldenRun",
    "GoldenScenario",
    "ScenarioError",
    "ScenarioInput",
    "apply_outages",
    "run_golden",
]
