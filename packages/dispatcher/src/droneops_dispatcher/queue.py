"""Queue fairness: priority order, aging, and a named starvation bound.

The problem this file exists for is the one every priority queue creates: a steady
stream of higher-priority work can postpone routine work forever, and "forever" is
not a number anyone chose. So the bound is chosen, named and versioned:

* ``policy.starvation_bound_us`` - the published cap on how long a mission may wait
  behind work of a higher class. At the bound the mission becomes *entitled*: it
  sorts ahead of everything that has not reached its own bound, and the round
  records a starvation event so the console can say which routine mission was
  rescued and by what rule.
* ``policy.aging_step_us`` - one rank of promotion per step, earned by waiting.
  Aging climbs the ladder but stops at ``life_critical_floor()``: waiting longer
  does not out-rank a verified life-critical mission or a safety contingency, and
  a commercial SLA earns at most ``max_sla_rank_promotions`` steps because a paid
  position is a discount on waiting, not a licence to jump.
* ties inside one band break earliest-deadline-first, then first-come-first-served,
  then mission id, so the order is a total order and a replay reproduces it.

Resequencing is the only thing a priority arrival may do, and only to unlaunched
work. :meth:`DispatchQueue.resequence` pins any mission that is airborne or holds a
reservation at its current index and records a refusal instead of moving it - see
:mod:`droneops_dispatcher.priority` for why no right exists to move it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from droneops_contracts import MissionStatus, PriorityClass

from .errors import FairnessPolicyError
from .policy import STARVATION_BOUND_NAME, DispatchPolicy, digest_of
from .priority import (
    MissionStatusRow,
    ResequenceRefusal,
    life_critical_floor,
    priority_rank,
    protection_verdict,
)


@dataclass(frozen=True)
class QueuedMission:
    """One mission waiting to be dispatched."""

    mission_id: str
    order_id: str
    priority: PriorityClass
    arrived_at_us: int
    deadline_us: int
    payload_g: int = 0
    status: MissionStatus = MissionStatus.ELIGIBLE
    holds_reservation: bool = False
    aircraft_id: str | None = None

    def __post_init__(self) -> None:
        if not self.mission_id:
            raise FairnessPolicyError("a queued mission needs an id")
        if self.deadline_us < self.arrived_at_us:
            raise FairnessPolicyError(
                f"{self.mission_id}: deadline {self.deadline_us} us precedes arrival "
                f"{self.arrived_at_us} us"
            )

    def waited_us(self, now_us: int) -> int:
        return max(0, now_us - self.arrived_at_us)

    def as_row(self) -> MissionStatusRow:
        return MissionStatusRow(
            mission_id=self.mission_id,
            priority=self.priority,
            status=self.status,
            aircraft_id=self.aircraft_id,
            holds_reservation=self.holds_reservation,
        )


@dataclass(frozen=True)
class FairnessRank:
    """What the fairness policy computed for one mission at one instant."""

    mission_id: str
    base_rank: int
    effective_rank: int
    aging_steps: int
    waited_us: int
    starved: bool
    entitled: bool
    sort_key: tuple[int, int, int, int, str]
    bound_us: int

    def as_row(self) -> tuple[str, int, int, int, int, bool, bool]:
        return (
            self.mission_id,
            self.base_rank,
            self.effective_rank,
            self.aging_steps,
            self.waited_us,
            self.starved,
            self.entitled,
        )

    def explanation(self, priority: PriorityClass) -> str:
        """Operator-facing sentence: the bound is named, not implied."""
        if self.entitled:
            return (
                f"{self.mission_id} waited {self.waited_us} us and reached the fairness "
                f"{STARVATION_BOUND_NAME} of {self.bound_us} us: it now goes ahead of "
                "unstarved work, and this event is recorded"
            )
        if self.aging_steps:
            return (
                f"{self.mission_id} earned {self.aging_steps} aging step(s) for waiting "
                f"{self.waited_us} us, climbing to rank {self.effective_rank} "
                f"(floor {life_critical_floor()})"
            )
        return (
            f"{self.mission_id} sits at rank {self.effective_rank} of "
            f"{priority.value}, waited {self.waited_us} us of {self.bound_us} us"
        )


@dataclass(frozen=True)
class ResequenceOutcome:
    """What a priority arrival actually did to the queue."""

    arriving_mission_id: str
    order_before: tuple[str, ...]
    order_after: tuple[str, ...]
    moved: tuple[tuple[str, int, int], ...]
    pinned: tuple[tuple[str, str], ...]
    refused: tuple[ResequenceRefusal, ...]
    outcome_digest: str

    def position_of(self, mission_id: str) -> int | None:
        return self.order_after.index(mission_id) if mission_id in self.order_after else None

    def moved_count(self) -> int:
        return len(self.moved)


class DispatchQueue:
    """The unlaunched backlog, ordered by the fairness policy."""

    def __init__(self, *, policy: DispatchPolicy) -> None:
        self.policy = policy
        self._items: list[QueuedMission] = []
        self._starved_seen: set[str] = set()

    def __len__(self) -> int:
        return len(self._items)

    @property
    def starvation_bound_us(self) -> int:
        return self.policy.starvation_bound_us

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(item.mission_id for item in self._items)

    def enqueue(self, mission: QueuedMission) -> None:
        if any(item.mission_id == mission.mission_id for item in self._items):
            raise FairnessPolicyError(f"{mission.mission_id} is already queued")
        self._items.append(mission)

    def remove(self, mission_id: str) -> QueuedMission | None:
        for index, item in enumerate(self._items):
            if item.mission_id == mission_id:
                del self._items[index]
                return item
        return None

    def get(self, mission_id: str) -> QueuedMission | None:
        for item in self._items:
            if item.mission_id == mission_id:
                return item
        return None

    def _promotions(self, mission: QueuedMission, waited_us: int) -> int:
        """Aging climbs inside the ordinary band only - never over life-critical work."""
        base = priority_rank(mission.priority)
        if base <= life_critical_floor():
            return 0
        earned = self.policy.aging_steps(waited_us)
        ceiling = base - (life_critical_floor() + 1)
        if mission.priority is PriorityClass.COMMERCIAL_SLA:
            ceiling = min(ceiling, self.policy.max_sla_rank_promotions)
        return max(0, min(earned, ceiling))

    def fairness(self, mission: QueuedMission, now_us: int) -> FairnessRank:
        """The rank record for one mission at ``now_us``, deterministic and total.

        Sorting is ``(band, entitlement, deadline, arrival, id)``: a verified
        life-critical or safety mission is in a band waiting can never reach, and
        inside the ordinary band a mission that reached the starvation bound goes
        before the un-starved work it was waiting behind.
        """
        waited = mission.waited_us(now_us)
        base = priority_rank(mission.priority)
        ordinary_band = base > life_critical_floor()
        steps = self._promotions(mission, waited)
        effective = max(base - steps, life_critical_floor() + 1) if ordinary_band else base
        starved = waited >= self.policy.starvation_bound_us
        return FairnessRank(
            mission_id=mission.mission_id,
            base_rank=base,
            effective_rank=effective,
            aging_steps=steps,
            waited_us=waited,
            starved=starved,
            entitled=starved and ordinary_band,
            sort_key=(
                effective,
                0 if (starved and ordinary_band) else 1,
                mission.deadline_us,
                mission.arrived_at_us,
                mission.mission_id,
            ),
            bound_us=self.policy.starvation_bound_us,
        )

    def ranking(self, now_us: int) -> tuple[FairnessRank, ...]:
        return tuple(self.fairness(mission, now_us) for mission in self.order(now_us))

    @staticmethod
    def _place(
        pinned: Sequence[tuple[int, QueuedMission]],
        total: int,
        ranked: Sequence[QueuedMission],
    ) -> tuple[QueuedMission, ...]:
        """Slot the ranked work around the missions that stay where they are."""
        placed: list[QueuedMission | None] = [None] * total
        for index, mission in pinned:
            placed[min(index, total - 1)] = mission
        free_indices = [index for index, item in enumerate(placed) if item is None]
        for index, mission in zip(free_indices, ranked, strict=True):
            placed[index] = mission
        return tuple(item for item in placed if item is not None)

    def _pinned_rows(self) -> tuple[tuple[int, QueuedMission], ...]:
        return tuple(
            (index, mission)
            for index, mission in enumerate(self._items)
            if mission.as_row().is_protected()
        )

    def order(self, now_us: int) -> tuple[QueuedMission, ...]:
        """Serve order under the fairness policy, with protected work pinned.

        A mission that is airborne or already committed keeps the index it had: the
        queue cannot resequence it either, because resequencing an order is how this
        system moves work, and moving an airborne commitment is not on the list.
        """
        pinned = self._pinned_rows()
        pinned_ids = {mission.mission_id for _, mission in pinned}
        ranked = sorted(
            (mission for mission in self._items if mission.mission_id not in pinned_ids),
            key=lambda mission: self.fairness(mission, now_us).sort_key,
        )
        return self._place(pinned, len(self._items), ranked)

    def starved(self, now_us: int) -> tuple[FairnessRank, ...]:
        """Missions at or past the bound - the starvation events of this round."""
        ranks = [self.fairness(mission, now_us) for mission in self._items]
        entitled = tuple(row for row in ranks if row.entitled)
        fresh = tuple(row for row in entitled if row.mission_id not in self._starved_seen)
        self._starved_seen.update(row.mission_id for row in entitled)
        return tuple(sorted(fresh, key=lambda row: row.mission_id))

    def resequence(
        self, arriving: QueuedMission, *, now_us: int, requested_by: PriorityClass | None = None
    ) -> ResequenceOutcome:
        """Insert one arrival and let it overtake *unlaunched* work only.

        Protected missions - airborne, in progress, or already holding a
        reservation - keep the index they had. Every attempt to pass one is
        reported as a refusal with the rule that stopped it, so the answer to
        "why did the priority mission not go first?" is a record, not a shrug.
        """
        before = self.ids
        arrival = arriving
        pinned_rows = self._pinned_rows()
        pinned_ids = {mission.mission_id for _, mission in pinned_rows}
        free = [mission for mission in self._items if mission.mission_id not in pinned_ids]
        pool = sorted(
            [*free, arrival],
            key=lambda mission: self.fairness(mission, now_us).sort_key,
        )
        placed_rows = self._place(pinned_rows, len(self._items) + 1, pool)
        after = tuple(mission.mission_id for mission in placed_rows)

        refusal_rows: list[ResequenceRefusal] = []
        arrival_position = after.index(arrival.mission_id)
        for index, mission in pinned_rows:
            if index > arrival_position:
                continue
            verdict = protection_verdict(arrival.as_row(), mission.as_row(), self.policy)
            if verdict is not None:
                refusal_rows.append(verdict)
        requested = requested_by if requested_by is not None else arrival.priority
        if requested is not arrival.priority:
            raise FairnessPolicyError("the requesting class must be the arriving mission's own")

        moved: list[tuple[str, int, int]] = []
        for mission_id in after:
            old_index = before.index(mission_id) if mission_id in before else -1
            new_index = after.index(mission_id)
            if old_index != new_index:
                moved.append((mission_id, old_index, new_index))

        self._items = list(placed_rows)
        digest = digest_of(
            [
                ("arriving", arrival.mission_id),
                ("before", ",".join(before)),
                ("after", ",".join(after)),
                ("pinned", ",".join(mission.mission_id for _, mission in pinned_rows)),
                (
                    "refused",
                    ";".join(
                        f"{row.requesting_mission_id}>{row.target_mission_id}"
                        for row in refusal_rows
                    ),
                ),
                ("bound_us", self.policy.starvation_bound_us),
                ("fairness_version", self.policy.fairness_policy_version),
                ("now_us", now_us),
            ]
        )
        return ResequenceOutcome(
            arriving_mission_id=arrival.mission_id,
            order_before=before,
            order_after=after,
            moved=tuple(sorted(moved, key=lambda row: row[0])),
            pinned=tuple(
                sorted((mission.mission_id, mission.status.value) for _, mission in pinned_rows)
            ),
            refused=tuple(
                sorted(refusal_rows, key=lambda row: (row.target_mission_id, row.code)),
            ),
            outcome_digest=digest,
        )

    def metrics(self, now_us: int) -> tuple[tuple[str, object], ...]:
        ranks = [self.fairness(mission, now_us) for mission in self._items]
        waits = sorted(row.waited_us for row in ranks)
        by_class: dict[str, int] = {}
        for mission in self._items:
            by_class[mission.priority.value] = by_class.get(mission.priority.value, 0) + 1
        return (
            ("queued", len(self._items)),
            ("starved", sum(1 for row in ranks if row.entitled)),
            ("queue_age_max_us", waits[-1] if waits else 0),
            ("queue_age_median_us", waits[len(waits) // 2] if waits else 0),
            ("priority_impact_on_ordinary", sum(1 for row in ranks if row.aging_steps)),
            *[(f"by_class_{key}", value) for key, value in sorted(by_class.items())],
        )


def queued_from(rows: Sequence[QueuedMission]) -> tuple[QueuedMission, ...]:
    """Deterministic intake order: arrival, then priority rank, then mission id."""
    return tuple(
        sorted(
            rows,
            key=lambda row: (
                row.arrived_at_us,
                priority_rank(row.priority),
                row.mission_id,
            ),
        )
    )


__all__ = [
    "DispatchQueue",
    "FairnessRank",
    "QueuedMission",
    "ResequenceOutcome",
    "queued_from",
]
