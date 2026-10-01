"""facilities: pads, chargers, loading and crew window scheduling (Phase 3).

This package turns site capacity into schedulable resources. A landing pad, a
charger bay and a crew slot are each a unit resource addressed by a canonical
geom-core key; the turnaround chain - approach, landing, handover/transfer,
loading, departure, plus recharge on a separate resource - is placed against
opening hours, declared outages and crew duty windows; and the result is a
*proposal*. Only the reservation ledger commits, and nothing in here can edit a
committed claim (``conventions.md`` sections 5 and 7).

What it guarantees:

* **queueing, never overbooking (I6).** Capacity is the count of distinct unit
  identities, and a unit holds one claim at a time, so the peak simultaneity
  across a site can never exceed its declared pads, bays or crew slots. When
  capacity is short the answer is a queue entry with a reason or a rejection
  with a reason - there is no third outcome.
* **reason-coded waits.** Pad wait, crew wait and charger wait are attributed at
  the moment they are discovered and stay separate all the way into metrics,
  because "pad free but no crew" and "crew free but charger occupied" are the
  realistic failures, not hypothetical ones.
* **an explicit outage asymmetry.** An outaged site accepts nothing new and
  preserves what is already committed; :class:`OutageImpact` cannot be built to
  say otherwise.

What it does not own: telemetry truth (import-linter forbids the import), aircraft
assignment, priority, pricing, or a repositioning leg. :mod:`droneops_facilities.repositioning`
surfaces alternate-site *data* for the dispatcher to price and explicitly
declines to choose.

Handover is ``LAND_TRANSFER`` only; ``WINCH`` and ``PARACHUTE`` are rejected at
construction, mirroring ``droneops_contracts``. All times are integer
microseconds, no clock is read, and no dict or set is iterated unordered.
"""

from __future__ import annotations

from .audit import UNIT_CAPACITY, CapacityAudit, PurposeAudit, audit_claims, audit_purpose
from .bundle import (
    DEFAULT_APPROACH_US,
    DEFAULT_CREW_SLOTS,
    FACILITIES_DOCUMENT,
    load_capacity_profiles,
    minutes_to_us,
    require_land_transfer,
    scenario_day,
)
from .errors import (
    BundleLoadError,
    CapacityError,
    CommitConflictError,
    FacilitiesError,
    HandoverTypeError,
    OutageError,
    ProposalError,
)
from .golden import (
    I6_INVARIANT,
    GoldenRun,
    GoldenScenario,
    ScenarioError,
    ScenarioInput,
    apply_outages,
    run_golden,
)
from .model import (
    SUPPORTED_HANDOVER,
    TURNAROUND_CHAIN,
    Accessibility,
    CapacityProfile,
    ClaimPurpose,
    DecisionState,
    FacilityCapacityView,
    OutageImpact,
    OutageState,
    OutageWindow,
    Rejection,
    ResourceClaim,
    SchedulingDecision,
    StageWindow,
    TurnaroundDurations,
    TurnaroundPlan,
    TurnaroundRequest,
    TurnaroundStage,
    WaitEvent,
    WaitReason,
    group_claims,
    purpose_for_kind,
    require_supported_handover,
)
from .occupancy import AppendOnlyLedger, CommittedOccupancy, CommitReceipt, Occupancy, ProposalBook
from .queueing import FacilityQueue, QueueEntry, QueueMetrics, order_arrivals, queue_metrics, waits_from
from .repositioning import (
    AlternateSiteQuote,
    AvailabilityCounts,
    alternate_site_quotes,
    availability_at,
    next_crew_open_us,
    quote_for,
)
from .resources import (
    UnitResource,
    canonical_keys,
    capacity_is_honoured,
    charger_resources,
    claim_windows,
    crew_resources,
    overlap_count,
    pad_resources,
    unit_resources,
    windows_of,
)
from .scheduler import (
    BatchOutcome,
    FacilityScheduler,
    PadPool,
    PadPoolOutcome,
    RejectedWindow,
    commit_many,
    profile_in_outage,
)

__all__ = [
    "DEFAULT_APPROACH_US",
    "DEFAULT_CREW_SLOTS",
    "FACILITIES_DOCUMENT",
    "I6_INVARIANT",
    "SUPPORTED_HANDOVER",
    "TURNAROUND_CHAIN",
    "UNIT_CAPACITY",
    "Accessibility",
    "AlternateSiteQuote",
    "AppendOnlyLedger",
    "AvailabilityCounts",
    "BatchOutcome",
    "BundleLoadError",
    "CapacityAudit",
    "CapacityError",
    "CapacityProfile",
    "ClaimPurpose",
    "CommittedOccupancy",
    "CommitReceipt",
    "CommitConflictError",
    "DecisionState",
    "FacilitiesError",
    "FacilityCapacityView",
    "FacilityQueue",
    "FacilityScheduler",
    "GoldenRun",
    "GoldenScenario",
    "HandoverTypeError",
    "Occupancy",
    "OutageError",
    "OutageImpact",
    "OutageState",
    "OutageWindow",
    "PadPool",
    "PadPoolOutcome",
    "ProposalBook",
    "ProposalError",
    "PurposeAudit",
    "QueueEntry",
    "QueueMetrics",
    "RejectedWindow",
    "Rejection",
    "ResourceClaim",
    "ScenarioError",
    "ScenarioInput",
    "SchedulingDecision",
    "StageWindow",
    "TurnaroundDurations",
    "TurnaroundPlan",
    "TurnaroundRequest",
    "TurnaroundStage",
    "UnitResource",
    "WaitEvent",
    "WaitReason",
    "alternate_site_quotes",
    "apply_outages",
    "audit_claims",
    "audit_purpose",
    "availability_at",
    "canonical_keys",
    "capacity_is_honoured",
    "claim_windows",
    "commit_many",
    "group_claims",
    "load_capacity_profiles",
    "minutes_to_us",
    "next_crew_open_us",
    "order_arrivals",
    "pad_resources",
    "profile_in_outage",
    "purpose_for_kind",
    "queue_metrics",
    "quote_for",
    "require_land_transfer",
    "require_supported_handover",
    "run_golden",
    "scenario_day",
    "unit_resources",
    "charger_resources",
    "crew_resources",
    "overlap_count",
    "waits_from",
    "windows_of",
]
