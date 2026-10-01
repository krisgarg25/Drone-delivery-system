"""The facility scheduler: turn an arrival into a chain of proposed windows.

The algorithm is a first-fit search over **start times**, not over slots:

1. the search window is ``ready`` clipped into the site's opening hours, minus
   declared outages;
2. for each unit resource of each purpose, subtract what is already committed or
   offered, and keep every gap with room - each survivor is an interval of
   feasible *start instants* for that stage;
3. the pad and the crew are resolved together, because the crewed span sits at a
   fixed offset inside the pad claim: a crew start window shifted back by
   approach-plus-landing becomes a constraint on the pad start. The minimum
   feasible start wins, tie-broken by lowest unit index;
4. recharge is then placed at the first free charger instant at or after pad
   release;
5. every delay is attributed to exactly one reason, so pad wait, crew wait and
   charger wait stay separately measurable.

Nothing here writes to the ledger. A successful proposal is recorded in the
:class:`~droneops_facilities.occupancy.ProposalBook` so the rest of the same
batch cannot be offered the same pad; the ledger settles it later, or does not,
and an unsettled offer only ever costs capacity - it never double-books.

Why earliest-start-then-first-fit rather than an optimal chain: the Phase 3 exit
criterion is "full pads and chargers create queueing, not overbooking". A search
that optimised completion time would need pinned tie-breaks anyway to replay
identically, so the policy is stated plainly, and the trade-off (a later pad
start might reach a charger sooner) is handed to the dispatcher as data.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from typing import Protocol, runtime_checkable

from droneops_contracts.facilities import FacilityStatus
from droneops_geom_core import Interval, checked_add, gaps_within

from .audit import CapacityAudit, audit_claims
from .errors import CapacityError
from .model import (
    CapacityProfile,
    ClaimPurpose,
    DecisionState,
    FacilityCapacityView,
    OutageImpact,
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
)
from .occupancy import CommittedOccupancy, Occupancy, ProposalBook
from .queueing import FacilityQueue, QueueEntry, order_arrivals, queue_metrics
from .resources import UnitResource, overlap_count, pad_resources


@dataclass(frozen=True)
class _Allowed:
    """One unit's feasible start instants for one stage."""

    unit: UnitResource
    starts: Interval


@dataclass(frozen=True)
class _Chain:
    """A solved pad-plus-crew placement, before the charger is considered."""

    start_us: int
    pad: UnitResource
    crew: UnitResource | None


@runtime_checkable
class ClaimListing(Protocol):
    """An occupancy source rich enough to list the claims it holds."""

    def committed_claims(self, facility_id: str | None = None) -> tuple[ResourceClaim, ...]: ...


@dataclass(frozen=True)
class BatchOutcome:
    """The result of one scheduling round: what got a window and what waited."""

    scheduled: tuple[TurnaroundPlan, ...]
    queued: tuple[QueueEntry, ...]
    rejected: tuple[Rejection, ...]
    audits: tuple[CapacityAudit, ...]
    metrics: dict[str, str | int]

    @property
    def i6_holds(self) -> bool:
        return all(audit.i6_holds for audit in self.audits)


class FacilityScheduler:
    """Pads, chargers, crew windows and loading, as one capacity problem."""

    def __init__(
        self,
        profiles: Iterable[CapacityProfile],
        *,
        committed: CommittedOccupancy | None = None,
        queue: FacilityQueue | None = None,
    ) -> None:
        indexed = {profile.facility_id: profile for profile in profiles}
        if not indexed:
            raise CapacityError("the facility scheduler needs at least one capacity profile")
        self._profiles: dict[str, CapacityProfile] = indexed
        self._book = ProposalBook()
        self._occupancy = Occupancy(committed if committed is not None else _EmptyOccupancy(), self._book)
        self._queue = queue if queue is not None else FacilityQueue()
        self._horizon = self._derive_horizon()
        self._events: list[WaitEvent] = []
        self._decisions: list[SchedulingDecision] = []

    # -- reads ---------------------------------------------------------------
    @property
    def horizon(self) -> Interval:
        return self._horizon

    @property
    def queue(self) -> FacilityQueue:
        return self._queue

    def profile_for(self, facility_id: str) -> CapacityProfile | None:
        return self._profiles.get(facility_id)

    def capacity_views(self) -> tuple[FacilityCapacityView, ...]:
        """The section 11 capacity list for every site, in facility-id order."""
        return tuple(
            self._profiles[facility_id].capacity_view(self._horizon) for facility_id in sorted(self._profiles)
        )

    def occupied(self, identity_token: str) -> tuple[Interval, ...]:
        """Committed plus still-unsettled offers on one resource."""
        return self._occupancy.occupied(identity_token)

    def outstanding(self) -> tuple[TurnaroundPlan, ...]:
        """Proposals awaiting the ledger: the handover this package makes."""
        return self._book.pending()

    def settle(self, proposal_id: str) -> TurnaroundPlan | None:
        """Note that the ledger took a proposal; the ledger owns that fact."""
        return self._book.settle(proposal_id)

    def claims_in_play(self, facility_id: str | None = None) -> tuple[ResourceClaim, ...]:
        """Every claim this scheduler has offered and the ledger has not settled."""
        out: list[ResourceClaim] = []
        for plan in self._book.pending():
            out.extend(plan.claims)
        if facility_id is not None:
            out = [claim for claim in out if claim.facility_id == facility_id]
        return tuple(sorted(out, key=lambda item: (item.identity_token, item.window.start, item.request_id)))

    def audit(self, facility_id: str) -> CapacityAudit:
        """I6 for one site, measured over the claims actually in play."""
        profile = self._profiles[facility_id]
        return audit_claims(
            self._all_claims_for(profile),
            facility_id=facility_id,
            pad_capacity=profile.pad_count,
            charger_capacity=profile.charger_bays,
            crew_capacity=profile.crew_slots,
        )

    def audit_all(self) -> tuple[CapacityAudit, ...]:
        return tuple(self.audit(facility_id) for facility_id in sorted(self._profiles))

    def outage_impact(self, facility_id: str, at_us: int) -> OutageImpact:
        """What an outage at ``at_us`` does: blocks new work, keeps committed work."""
        profile = self._profiles[facility_id]
        return profile.impact_of_outage(at_us, self._all_claims_for(profile))

    # -- proposals -----------------------------------------------------------
    def propose(self, request: TurnaroundRequest) -> SchedulingDecision:
        """One arrival in; a proposal, a queue entry or a reason-coded rejection.

        Identical input produces an identical :attr:`TurnaroundPlan.proposal_id`,
        so a retried handover cannot create a second commitment (I2), and the
        queue holds one entry per request id however often it is re-attempted.
        """
        decision = self._decide(request)
        self._decisions.append(decision)
        if decision.plan is not None:
            self._events.extend(decision.plan.waits)
            self._book.offer(decision.plan)
            self._queue.remove(request.request_id)
        return decision

    def schedule(self, requests: Iterable[TurnaroundRequest]) -> BatchOutcome:
        """A batch, served in the documented arrival order."""
        plans: list[TurnaroundPlan] = []
        queued: list[QueueEntry] = []
        rejected: list[Rejection] = []
        events: list[WaitEvent] = []
        for request in order_arrivals(requests):
            before = len(self._events)
            decision = self.propose(request)
            # Waits are recorded whether the work was granted or refused: a plan
            # that started late carries its own queue time, and a refusal carries
            # the wait until the estimate. Both belong in the batch totals.
            events.extend(self._events[before:])
            if decision.plan is not None:
                plans.append(decision.plan)
                continue
            # A queued request still carries its rejection detail, so the state -
            # not the presence of a rejection - decides which bucket it counts in.
            if decision.state is DecisionState.REJECTED and decision.rejection is not None:
                rejected.append(decision.rejection)
            entry = self._queue.entry(request.request_id)
            if entry is not None:
                queued.append(entry)
        metrics = queue_metrics(
            scheduled=len(plans),
            queue=self._queue,
            rejections=rejected,
            events=events,
        ).as_metrics()
        return BatchOutcome(
            scheduled=tuple(plans),
            queued=tuple(queued),
            rejected=tuple(rejected),
            audits=self.audit_all(),
            metrics=metrics,
        )

    def metrics(self) -> dict[str, str | int]:
        """Batch metrics over everything this scheduler has decided."""
        return queue_metrics(
            scheduled=sum(1 for decision in self._decisions if decision.granted),
            queue=self._queue,
            rejections=tuple(
                decision.rejection
                for decision in self._decisions
                if decision.state is DecisionState.REJECTED and decision.rejection is not None
            ),
            events=self._events,
        ).as_metrics()

    # -- the search ----------------------------------------------------------
    def _decide(self, request: TurnaroundRequest) -> SchedulingDecision:
        profile = self._profiles.get(request.facility_id)
        if profile is None:
            return self._refuse(request, WaitReason.NO_SUCH_FACILITY, "no capacity profile for this facility")
        if request.policy_version != profile.policy_version:
            return self._refuse(
                request,
                WaitReason.POLICY_VERSION_MISMATCH,
                f"request policy {request.policy_version!r} against site policy "
                f"{profile.policy_version!r}: capacity numbers are not comparable",
            )
        if request.handover_type is not profile.accessibility.handover_type:
            return self._refuse(
                request,
                WaitReason.HANDOVER_TYPE_UNSUPPORTED,
                f"site {profile.facility_id} only performs "
                f"{profile.accessibility.handover_type.value!r} handover",
            )
        if profile.pad_count == 0:
            return self._refuse(request, WaitReason.PAD_WAIT, "the site declares no pads at all")

        durations = profile.durations_for(request.durations)
        lower = max(request.ready_us, profile.opens_us)
        upper = min(request.deadline_us, profile.closes_us, self._horizon.end)
        if upper <= lower:
            reason = (
                WaitReason.OUTSIDE_OPENING_HOURS
                if request.ready_us >= profile.closes_us or request.deadline_us <= profile.opens_us
                else WaitReason.HORIZON_EXCEEDED
            )
            return self._refuse(request, reason, "no time left inside the site's opening hours")
        search = Interval.of(lower, upper)
        usable = profile.usable_windows(search)
        if not usable:
            outage = profile.outage_covering(search)
            if outage is None:
                return self._refuse(request, WaitReason.OUTSIDE_OPENING_HOURS, "no open window remains")
            return self._refuse(
                request,
                WaitReason.FACILITY_OUTAGE,
                f"outage {outage.window} covers the request; new claims are refused while "
                f"committed claims stay on the record",
            )

        chain_length = durations.total_us
        pad_allowed = self._allowed_starts(profile.pads(), usable, chain_length)
        if not pad_allowed:
            return self._refuse(
                request,
                WaitReason.PAD_WAIT,
                f"all {profile.pad_count} pads are busy for the whole {chain_length} us chain",
                units=profile.pads(),
                duration_us=chain_length,
                from_us=lower,
                upper=upper,
            )

        crew_total = durations.crew_us
        chain = self._place_chain(profile, pad_allowed, usable, search, durations, crew_total)
        if chain is None:
            return self._refuse(
                request,
                WaitReason.CREW_UNAVAILABLE,
                f"a pad is free but no crew window covers handover plus loading of {crew_total} us",
                units=profile.pads(),
                duration_us=chain_length,
                from_us=lower,
                upper=upper,
            )

        pad_earliest = min(item.starts.start for item in pad_allowed)
        waits: list[WaitEvent] = []
        if pad_earliest > lower:
            waits.append(WaitEvent(request.request_id, WaitReason.PAD_WAIT, lower, pad_earliest, "pads"))
        if chain.start_us > pad_earliest:
            waits.append(
                WaitEvent(request.request_id, WaitReason.CREW_UNAVAILABLE, pad_earliest, chain.start_us, "crew")
            )

        charger: tuple[UnitResource, int] | None = None
        recharge_us = request.recharge_us if request.requires_recharge else 0
        if recharge_us > 0:
            release = checked_add(chain.start_us, chain_length)
            charge_windows = _clip(usable, (Interval.of(release, upper),))
            allowed = self._allowed_starts(profile.chargers(), charge_windows, recharge_us)
            if not allowed:
                return self._refuse(
                    request,
                    WaitReason.CHARGER_WAIT,
                    f"pad and crew are available but no charger bay has {recharge_us} us free "
                    f"before closing at {upper}",
                    units=profile.chargers(),
                    duration_us=recharge_us,
                    from_us=release,
                    upper=upper,
                )
            best = min(allowed, key=lambda item: (item.starts.start, item.unit.index))
            charger = (best.unit, best.starts.start)
            if best.starts.start > release:
                waits.append(
                    WaitEvent(
                        request.request_id,
                        WaitReason.CHARGER_WAIT,
                        release,
                        best.starts.start,
                        best.unit.identity_token(),
                    )
                )
        return SchedulingDecision(
            state=DecisionState.SCHEDULED,
            request_id=request.request_id,
            facility_id=profile.facility_id,
            reason=WaitReason.NONE,
            plan=self._build_plan(request, profile, durations, chain, charger, waits),
        )

    def _place_chain(
        self,
        profile: CapacityProfile,
        pad_allowed: tuple[_Allowed, ...],
        usable: tuple[Interval, ...],
        search: Interval,
        durations: TurnaroundDurations,
        crew_total: int,
    ) -> _Chain | None:
        """The earliest start at which both a pad and, if needed, crew can serve."""
        if not profile.accessibility.requires_crew or crew_total == 0:
            best = min(pad_allowed, key=lambda item: (item.starts.start, item.unit.index))
            return _Chain(best.starts.start, best.unit, None)
        crew_units = profile.crews()
        if not crew_units:
            return None
        crew_allowed = self._allowed_starts(
            crew_units, _clip(usable, profile.crew_windows(search)), crew_total
        )
        if not crew_allowed:
            return None
        pre_crew = durations.pre_crew_us
        best_key: tuple[int, int, int] | None = None
        winner: tuple[UnitResource, UnitResource] | None = None
        for pad in pad_allowed:
            for crew in crew_allowed:
                # The crew span must begin at pad_start + approach + landing, so
                # a crew window admits pad starts shifted earlier by that offset.
                low = max(pad.starts.start, crew.starts.start - pre_crew)
                high = min(pad.starts.end, crew.starts.end - pre_crew)
                if low >= high:
                    continue
                key = (low, pad.unit.index, crew.unit.index)
                if best_key is None or key < best_key:
                    best_key = key
                    winner = (pad.unit, crew.unit)
        if best_key is None or winner is None:
            return None
        return _Chain(best_key[0], winner[0], winner[1])

    def _allowed_starts(
        self, units: tuple[UnitResource, ...], windows: tuple[Interval, ...], duration_us: int
    ) -> tuple[_Allowed, ...]:
        """Feasible start intervals for ``duration_us`` on each free unit."""
        out: list[_Allowed] = []
        for unit in units:
            busy = self._occupancy.occupied(unit.identity_token())
            for window in windows:
                for gap in gaps_within(window, busy):
                    last_start = gap.end - duration_us
                    if last_start >= gap.start:
                        out.append(_Allowed(unit, Interval.of(gap.start, checked_add(last_start, 1))))
        return tuple(out)

    def _build_plan(
        self,
        request: TurnaroundRequest,
        profile: CapacityProfile,
        durations: TurnaroundDurations,
        chain: _Chain,
        charger: tuple[UnitResource, int] | None,
        waits: list[WaitEvent],
    ) -> TurnaroundPlan:
        start = chain.start_us
        end = checked_add(start, durations.total_us)
        pad_window = Interval.of(start, end)
        crew_window: Interval | None = None
        if chain.crew is not None and durations.crew_us > 0:
            crew_low = checked_add(start, durations.pre_crew_us)
            crew_window = Interval.of(crew_low, checked_add(crew_low, durations.crew_us))

        stages: list[StageWindow] = []
        crew_unit = chain.crew if crew_window is not None else None
        for stage, offset in durations.offsets():
            window = Interval.of(checked_add(start, offset.start), checked_add(start, offset.end))
            # A crewed stage is still physically inside the pad claim: the pad
            # unit is held for the whole chain, and the crew unit names the extra
            # constraint that has to be satisfied for the stage to run.
            on_crew = crew_unit is not None and crew_window is not None and crew_window.contains(window)
            stages.append(
                StageWindow(
                    stage=stage,
                    window=window,
                    facility_id=profile.facility_id,
                    unit_identifier=crew_unit.identifier if on_crew and crew_unit else chain.pad.identifier,
                    purpose=ClaimPurpose.CREW if on_crew and crew_unit else ClaimPurpose.PAD,
                )
            )
        claims: list[ResourceClaim] = [
            ResourceClaim(
                request_id=request.request_id,
                holder=request.holder,
                facility_id=profile.facility_id,
                unit=chain.pad,
                window=pad_window,
                purpose=ClaimPurpose.PAD,
                stages=tuple(
                    item.stage for item in stages if item.purpose is ClaimPurpose.PAD
                ),
            )
        ]
        if chain.crew is not None and crew_window is not None:
            claims.append(
                ResourceClaim(
                    request_id=request.request_id,
                    holder=request.holder,
                    facility_id=profile.facility_id,
                    unit=chain.crew,
                    window=crew_window,
                    purpose=ClaimPurpose.CREW,
                    stages=(TurnaroundStage.HANDOVER, TurnaroundStage.LOADING),
                )
            )
        if charger is not None and request.recharge_us > 0:
            unit, charge_start = charger
            charge_window = Interval.of(charge_start, checked_add(charge_start, request.recharge_us))
            claims.append(
                ResourceClaim(
                    request_id=request.request_id,
                    holder=request.holder,
                    facility_id=profile.facility_id,
                    unit=unit,
                    window=charge_window,
                    purpose=ClaimPurpose.CHARGER,
                    stages=(TurnaroundStage.RECHARGE,),
                )
            )
            stages.append(
                StageWindow(
                    stage=TurnaroundStage.RECHARGE,
                    window=charge_window,
                    facility_id=profile.facility_id,
                    unit_identifier=unit.identifier,
                    purpose=ClaimPurpose.CHARGER,
                )
            )
        ordered_stages = tuple(sorted(stages, key=lambda item: (item.window.start, item.stage.value)))
        return TurnaroundPlan(
            request_id=request.request_id,
            facility_id=profile.facility_id,
            holder=request.holder,
            policy_version=profile.policy_version,
            stages=ordered_stages,
            claims=tuple(sorted(claims, key=lambda item: (item.purpose.value, item.identity_token))),
            waits=tuple(waits),
            start_us=start,
            end_us=end,
        )

    # -- refusal -------------------------------------------------------------
    def _refuse(
        self,
        request: TurnaroundRequest,
        reason: WaitReason,
        detail: str,
        *,
        units: tuple[UnitResource, ...] = (),
        duration_us: int = 0,
        from_us: int | None = None,
        upper: int | None = None,
    ) -> SchedulingDecision:
        retry = (
            _earliest_free(from_us, duration_us, units, self.occupied, upper)
            if units and duration_us and from_us is not None and upper is not None
            else None
        )
        rejection = Rejection(
            request_id=request.request_id,
            facility_id=request.facility_id,
            reason=reason,
            detail=detail,
            earliest_retry_us=retry,
        )
        if not request.allow_queue:
            return SchedulingDecision(
                state=DecisionState.REJECTED,
                request_id=request.request_id,
                facility_id=request.facility_id,
                reason=reason,
                rejection=rejection,
            )
        self._queue.enqueue(
            QueueEntry(
                request=request,
                reason=reason,
                queued_at_us=request.ready_us,
                blocked_until_us=retry,
            )
        )
        if retry is not None and retry > request.ready_us:
            self._events.append(
                WaitEvent(
                    request_id=request.request_id,
                    reason=reason,
                    from_us=request.ready_us,
                    to_us=retry,
                    blocked_on=rejection.facility_id,
                )
            )
        return SchedulingDecision(
            state=DecisionState.QUEUED,
            request_id=request.request_id,
            facility_id=request.facility_id,
            reason=reason,
            rejection=rejection,
        )

    def _all_claims_for(self, profile: CapacityProfile) -> tuple[ResourceClaim, ...]:
        committed = (
            self._occupancy.committed.committed_claims(profile.facility_id)
            if isinstance(self._occupancy.committed, ClaimListing)
            else ()
        )
        return _dedupe((*committed, *self.claims_in_play(profile.facility_id)))

    def _derive_horizon(self) -> Interval:
        opens = min(profile.opens_us for profile in self._profiles.values())
        closes = max(profile.closes_us for profile in self._profiles.values())
        return Interval.of(opens, closes)


class _EmptyOccupancy:
    """Nothing committed yet: a run that starts from an empty ledger."""

    def committed_windows(self, identity_token: str) -> tuple[Interval, ...]:
        return ()


def _clip(windows: tuple[Interval, ...], mask: tuple[Interval, ...]) -> tuple[Interval, ...]:
    """Windows that survive a mask, ascending and non-empty."""
    out: list[Interval] = []
    for window in windows:
        for held in mask:
            out.extend(piece for piece in window.intersection(held) if not piece.is_empty)
    return tuple(sorted(out, key=lambda item: (item.start, item.end)))


def _earliest_free(
    from_us: int,
    duration_us: int,
    units: tuple[UnitResource, ...],
    occupied: Callable[[str], tuple[Interval, ...]],
    upper: int | None,
) -> int | None:
    """The first instant at or after ``from_us`` where some unit has room.

    Candidates are the request time plus every busy-window end, so the scan is
    finite and its answer depends only on the data, never on iteration order.
    ``None`` means nothing frees before the horizon; it is an estimate for the
    queue report, and the next proposal re-derives the real window.
    """
    if upper is None or duration_us <= 0:
        return None
    seeds: set[int] = {from_us}
    for unit in units:
        seeds.update(held.end for held in occupied(unit.identity_token()))
    for candidate in sorted(value for value in seeds if value >= from_us):
        window = Interval.of(candidate, checked_add(candidate, duration_us))
        if window.end > upper:
            return None
        for unit in units:
            if not any(window.overlaps(held) for held in occupied(unit.identity_token())):
                return candidate
    return None


def _dedupe(claims: Iterable[ResourceClaim]) -> tuple[ResourceClaim, ...]:
    """Claims once per (identity, window, request): a settled offer is not two."""
    seen: dict[tuple[str, int, int, str], ResourceClaim] = {}
    for claim in claims:
        seen.setdefault((claim.identity_token, claim.window.start, claim.window.end, claim.request_id), claim)
    return tuple(seen[key] for key in sorted(seen))


def profile_in_outage(profile: CapacityProfile, outage: OutageWindow) -> CapacityProfile:
    """A new profile carrying an added outage; the old record is untouched.

    Facility state changes are superseding records, never in-place edits, which
    is what lets an audit show who believed what and when.
    """
    return replace(profile, status=FacilityStatus.OUTAGE, outages=(*profile.outages, outage))


# --- the pad pool: the spec's property-test surface -------------------------


@dataclass(frozen=True)
class RejectedWindow:
    """An arrival a pool could not host, with the obstruction that refused it."""

    index: int
    window: Interval
    reason: WaitReason
    blocked_by: tuple[tuple[str, Interval], ...]

    @property
    def obstructed(self) -> bool:
        """Every pad is covered somewhere across this window: true queueing."""
        return bool(self.blocked_by)


@dataclass(frozen=True)
class PadPoolOutcome:
    """What a pad pool accepted and what it made wait."""

    facility_id: str
    capacity: int
    commitments: tuple[ResourceClaim, ...]
    rejected: tuple[RejectedWindow, ...]

    @property
    def peak_overlap(self) -> int:
        return overlap_count(tuple(claim.window for claim in self.commitments))

    @property
    def holds_i6(self) -> bool:
        """``max(overlap_count(commitments)) <= capacity``."""
        return self.peak_overlap <= self.capacity


class PadPool:
    """``capacity`` single-occupancy pads, first-fit, in the documented order.

    This is the surface the property test in ``technical-build/06`` section 3
    exercises. A window is refused only when every pad already carries a claim
    overlapping it, so the pool can be late and can be asked to wait, but cannot
    be overbooked.
    """

    def __init__(
        self, facility_id: str, capacity: int, *, policy_version: str = "sim-facility-policy-v1"
    ) -> None:
        self.facility_id = facility_id
        self.capacity = capacity
        self.policy_version = policy_version
        self._units = pad_resources(facility_id, capacity)
        self._held: dict[str, list[Interval]] = {unit.identity_token(): [] for unit in self._units}
        self._commitments: list[ResourceClaim] = []
        self._rejected: list[RejectedWindow] = []

    @property
    def commitments(self) -> tuple[ResourceClaim, ...]:
        return tuple(self._commitments)

    @property
    def rejected(self) -> tuple[RejectedWindow, ...]:
        return tuple(self._rejected)

    @property
    def units(self) -> tuple[UnitResource, ...]:
        return self._units

    def held_windows(self, identity_token: str) -> tuple[Interval, ...]:
        return tuple(sorted(self._held.get(identity_token, ()), key=lambda item: (item.start, item.end)))

    def commit_many(self, arrival_windows: Iterable[Interval]) -> PadPoolOutcome:
        """Accept what fits, in arrival order; refuse the rest with a reason.

        Equal arrivals keep their input order (a stable sort of nothing - the
        sequence *is* the order), so a scenario that lists two simultaneous
        requests gets the same assignment on every replay.
        """
        self._rejected = []
        for index, window in enumerate(arrival_windows):
            unit = self._first_fit(window)
            if unit is None:
                self._rejected.append(
                    RejectedWindow(
                        index=index,
                        window=window,
                        reason=WaitReason.PAD_WAIT,
                        blocked_by=tuple(
                            sorted(
                                (
                                    (held_unit.identity_token(), held)
                                    for held_unit in self._units
                                    for held in self.held_windows(held_unit.identity_token())
                                    if held.overlaps(window)
                                ),
                                key=lambda item: (item[0], item[1].start, item[1].end),
                            )
                        ),
                    )
                )
                continue
            self._held.setdefault(unit.identity_token(), []).append(window)
            self._commitments.append(
                ResourceClaim(
                    request_id=f"{self.facility_id}:arrival-{index}",
                    holder=f"arrival-{index}",
                    facility_id=self.facility_id,
                    unit=unit,
                    window=window,
                    purpose=ClaimPurpose.PAD,
                )
            )
        return PadPoolOutcome(
            facility_id=self.facility_id,
            capacity=self.capacity,
            commitments=self.commitments,
            rejected=tuple(self._rejected),
        )

    def _first_fit(self, window: Interval) -> UnitResource | None:
        if window.is_empty:
            return None
        for unit in self._units:
            if not any(window.overlaps(held) for held in self.held_windows(unit.identity_token())):
                return unit
        return None


def commit_many(arrival_windows: Iterable[Interval], capacity: int) -> tuple[ResourceClaim, ...]:
    """The spec's ``scheduler.commit_many(arrival_windows, capacity)`` shape."""
    return PadPool("pad-pool", capacity).commit_many(arrival_windows).commitments


__all__ = [
    "BatchOutcome",
    "ClaimListing",
    "FacilityScheduler",
    "PadPool",
    "PadPoolOutcome",
    "RejectedWindow",
    "commit_many",
    "profile_in_outage",
]
