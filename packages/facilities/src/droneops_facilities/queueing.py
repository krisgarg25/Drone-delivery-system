"""Queueing and wait accounting: the mechanism behind "queue, never overbook".

When capacity is short there are exactly three honest answers, and this module
names them: a proposal (the work has a window), a queue entry (the work has no
window yet and is still waiting, with a reason), or a rejection (the work will
not fit before its deadline). "Overbook" is not an option in the enum, which is
the point.

Wait time is attributed to a single reason per event and the reasons are kept
separate all the way out to metrics, because Phase 1's report and Phase 4's
console show pad wait and charger wait as distinct causes. A queue entry can
carry the same reason twice across two rounds; the totals add, they do not
merge.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from .model import Rejection, TurnaroundRequest, WaitEvent, WaitReason


@dataclass(frozen=True)
class QueueEntry:
    """One request waiting for a reason."""

    request: TurnaroundRequest
    reason: WaitReason
    queued_at_us: int
    blocked_until_us: int | None
    attempts: int = 1

    def __post_init__(self) -> None:
        if self.reason is WaitReason.NONE:
            raise ValueError("a queue entry must name why it is waiting")
        if self.attempts < 1:
            raise ValueError("a queue entry has been attempted at least once")

    @property
    def request_id(self) -> str:
        return self.request.request_id

    def waited_us(self, now_us: int) -> int:
        """Time spent waiting as of ``now_us``. A parameter, never a clock read."""
        return max(0, now_us - self.queued_at_us)

    @property
    def arrival_order(self) -> tuple[int, int, str]:
        return self.request.arrival_order


@dataclass(frozen=True)
class QueueMetrics:
    """The reason-coded shape of one scheduling batch."""

    scheduled: int
    queued: int
    rejected: int
    pad_wait_us: int
    charger_wait_us: int
    crew_wait_us: int
    peak_queue: int
    wait_by_reason: tuple[tuple[str, int], ...]

    def as_metrics(self) -> dict[str, str | int]:
        flat: dict[str, str | int] = {
            "scheduled": self.scheduled,
            "queued": self.queued,
            "rejected": self.rejected,
            "pad_wait_us": self.pad_wait_us,
            "charger_wait_us": self.charger_wait_us,
            "crew_wait_us": self.crew_wait_us,
            "peak_queue": self.peak_queue,
            "total_requests": self.scheduled + self.queued + self.rejected,
        }
        for reason, total in self.wait_by_reason:
            flat[f"wait_us_{reason}"] = total
        return {key: flat[key] for key in sorted(flat)}


class FacilityQueue:
    """Arrival-ordered waiting work, with a starvation cap.

    Ordering is :attr:`TurnaroundRequest.arrival_order` - preference, then
    arrival instant, then canonical request id. Two requests with equal
    preference and equal arrival are separated by their id bytes, never by dict
    or set iteration, so the tie-break survives a replay on another machine.
    """

    def __init__(self, *, starvation_cap_us: int | None = None) -> None:
        self._entries: dict[str, QueueEntry] = {}
        self._peak = 0
        self.starvation_cap_us = starvation_cap_us

    def enqueue(self, entry: QueueEntry) -> None:
        """Add or re-queue work; a repeat attempt keeps its original wait start."""
        previous = self._entries.get(entry.request_id)
        record = entry
        if previous is not None:
            record = QueueEntry(
                request=entry.request,
                reason=entry.reason,
                queued_at_us=previous.queued_at_us,
                blocked_until_us=entry.blocked_until_us,
                attempts=previous.attempts + 1,
            )
        self._entries[entry.request_id] = record
        self._peak = max(self._peak, len(self._entries))

    def remove(self, request_id: str) -> QueueEntry | None:
        return self._entries.pop(request_id, None)

    def entry(self, request_id: str) -> QueueEntry | None:
        """The current entry for one request, without disturbing the order."""
        return self._entries.get(request_id)

    def pending(self) -> tuple[QueueEntry, ...]:
        """Waiting work in the documented service order."""
        return tuple(sorted(self._entries.values(), key=lambda entry: entry.arrival_order))

    def __len__(self) -> int:
        return len(self._entries)

    @property
    def peak_length(self) -> int:
        return self._peak

    def starved(self, now_us: int) -> tuple[QueueEntry, ...]:
        """Entries held past the cap - the starvation signal, not a preemption.

        A starved entry is reported, never promoted past a committed claim:
        priority cannot delete an airborne commitment (Phase 3 exit criterion).
        """
        if self.starvation_cap_us is None:
            return ()
        return tuple(
            entry
            for entry in self.pending()
            if entry.waited_us(now_us) > self.starvation_cap_us
        )

    def reasons(self) -> tuple[WaitReason, ...]:
        """Waiting causes, ordered and de-duplicated for reporting."""
        return tuple(sorted({entry.reason for entry in self._entries.values()}, key=lambda r: r.value))


def waits_from(events: Iterable[WaitEvent]) -> dict[WaitReason, int]:
    """Total microseconds per reason, reasons ordered by their code name."""
    totals: dict[WaitReason, int] = {}
    for event in sorted(events, key=lambda item: (item.reason.value, item.request_id, item.from_us)):
        totals[event.reason] = totals.get(event.reason, 0) + event.duration_us
    return totals


def queue_metrics(
    *,
    scheduled: int,
    queue: FacilityQueue,
    rejections: Iterable[Rejection],
    events: Iterable[WaitEvent],
) -> QueueMetrics:
    """The one place batch metrics are assembled, so reports cannot drift."""
    totals = waits_from(events)
    rejections = tuple(rejections)
    return QueueMetrics(
        scheduled=scheduled,
        queued=len(queue),
        rejected=len(rejections),
        pad_wait_us=totals.get(WaitReason.PAD_WAIT, 0),
        charger_wait_us=totals.get(WaitReason.CHARGER_WAIT, 0),
        crew_wait_us=totals.get(WaitReason.CREW_UNAVAILABLE, 0),
        peak_queue=queue.peak_length,
        wait_by_reason=tuple((reason.value, totals[reason]) for reason in sorted(totals, key=lambda r: r.value)),
    )


def order_arrivals(requests: Iterable[TurnaroundRequest]) -> tuple[TurnaroundRequest, ...]:
    """Requests in the documented service order. The only iteration order allowed."""
    return tuple(sorted(requests, key=lambda item: item.arrival_order))


__all__ = [
    "FacilityQueue",
    "QueueEntry",
    "QueueMetrics",
    "order_arrivals",
    "queue_metrics",
    "waits_from",
]
