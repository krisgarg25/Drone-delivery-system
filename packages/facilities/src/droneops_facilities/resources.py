"""Canonical facility resource keys and the overlap maths behind invariant I6.

Every pad, charger bay and crew slot is addressed as a **unit-capacity**
resource: ``pad-hub-central-synthetic-0`` holds one aircraft at a time, and a
facility with four pads owns four such identities. Capacity is therefore never a
counter that could be pushed past a limit; it is the *number of distinct
identities*, and an overlap on one identity is impossible by construction. That
is the whole mechanism behind "full pads create queueing, not overbooking"
(``technical-build/05`` Phase 3 exit criteria).

Key shapes follow ``technical-build/01`` section 6 through
:func:`droneops_geom_core.resource_key`, so the emitted prefix is the ledger
token (``pad``, ``charger``, ``crew``) and the ordering is the canonical lock
order ground facilities sit at the end of.

Identifiers deliberately match the scheme the planner already reads occupancy
under (``pad-<facility_id>-<index>``, ``charger-<facility_id>-<index>``), so a
facility proposal and a planner occupancy query name the same physical object.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from droneops_geom_core import (
    Interval,
    ResourceKey,
    ResourceKind,
    resource_key,
    sorted_resource_keys,
    validate_identifier,
)

from .errors import CapacityError

if TYPE_CHECKING:  # pragma: no cover - annotation-only
    from .model import ResourceClaim

PAD_INDEX_PREFIX: Final[str] = "pad"
CHARGER_INDEX_PREFIX: Final[str] = "charger"
CREW_INDEX_PREFIX: Final[str] = "crew"


@dataclass(frozen=True, order=True)
class UnitResource:
    """One physical, single-occupancy facility resource.

    Ordering is ``(kind.lock_rank, index)``, which is the scan order the
    scheduler uses: pads before chargers before crew, lowest index first. Fixing
    the scan order is what makes two identical requests land on two different
    units instead of racing for the same one.
    """

    kind: ResourceKind
    facility_id: str
    index: int
    prefix: str

    @property
    def identifier(self) -> str:
        """The stable object name, without any time window."""
        return validate_identifier(f"{self.prefix}-{self.facility_id}-{self.index}")

    def key(self, window: Interval) -> ResourceKey:
        """The canonical ledger key for this unit over ``window``."""
        return resource_key(self.kind, self.identifier, window)

    def identity_token(self) -> str:
        """The grouping token: every claim on this same physical object."""
        return f"{self.kind.prefix}:{self.identifier}"


def unit_resources(kind: ResourceKind, facility_id: str, count: int) -> tuple[UnitResource, ...]:
    """The ``count`` units of one kind, in the documented ascending scan order.

    ``count == 0`` yields ``()``: a hub with no charger bays is a normal
    configuration, not an error, and the scheduler reads the empty tuple as
    "recharge is impossible here".

    Raises:
        CapacityError: a negative count, which no scenario may encode.
    """
    if count < 0:
        raise CapacityError(f"{kind.prefix} count for {facility_id!r} is negative: {count}")
    prefix = _PREFIX_BY_KIND[kind]
    return tuple(UnitResource(kind, facility_id, index, prefix) for index in range(count))


_PREFIX_BY_KIND: Final[dict[ResourceKind, str]] = {
    ResourceKind.PAD: PAD_INDEX_PREFIX,
    ResourceKind.CHARGER_BAY: CHARGER_INDEX_PREFIX,
    ResourceKind.CREW: CREW_INDEX_PREFIX,
}


def pad_resources(facility_id: str, count: int) -> tuple[UnitResource, ...]:
    """The pad units of a facility, ascending."""
    return unit_resources(ResourceKind.PAD, facility_id, count)


def charger_resources(facility_id: str, count: int) -> tuple[UnitResource, ...]:
    """The charger bay units of a facility, ascending."""
    return unit_resources(ResourceKind.CHARGER_BAY, facility_id, count)


def crew_resources(facility_id: str, count: int) -> tuple[UnitResource, ...]:
    """The crew slot units of a facility, ascending."""
    return unit_resources(ResourceKind.CREW, facility_id, count)


def canonical_keys(claims: Iterable[ResourceClaim]) -> tuple[ResourceKey, ...]:
    """Claim keys in canonical lock-acquisition order, duplicates removed.

    A proposal lists its resources in this order before the ledger sees it, so
    the row-lock order in ``conventions.md`` section 5 step 7 is decided here
    and is identical on every replay.
    """
    return sorted_resource_keys(claim.key for claim in claims)


def windows_of(intervals: Iterable[Interval]) -> tuple[Interval, ...]:
    """The intervals, ascending by ``(start, end)``.

    The single ordering used for every capacity audit, so a report and a test
    compute the same peak from the same rows.
    """
    return tuple(sorted(intervals, key=lambda item: (item.start, item.end)))


def claim_windows(claims: Iterable[ResourceClaim]) -> tuple[Interval, ...]:
    """The windows a set of claims occupies, ascending.

    Empty claims are kept: :func:`overlap_count` ignores them, and dropping them
    here would hide a caller that proposed a zero-length stage.
    """
    return windows_of(claim.window for claim in claims)


def overlap_count(intervals: Iterable[Interval]) -> int:
    """The maximum number of intervals covering a single instant.

    A half-open sweep: an endpoint and a start at the same instant do not
    overlap, so ends are counted before starts. ``[]`` gives ``0`` and a single
    non-empty interval gives ``1``; empty intervals contribute nothing because
    they claim no instant.

    This is the left-hand side of invariant I6,
    ``max(overlap_count(commitments)) <= capacity``.
    """
    events: list[tuple[int, int]] = []
    for interval in intervals:
        if interval.is_empty:
            continue
        events.append((interval.start, 1))
        events.append((interval.end, -1))
    # (time, delta) ascending puts -1 before +1 at an equal instant: adjacency
    # is not overlap, which is the documented interval semantics.
    events.sort()
    active = 0
    peak = 0
    for _, delta in events:
        active += delta
        peak = max(peak, active)
    return peak


def capacity_is_honoured(intervals: Iterable[Interval], capacity: int) -> bool:
    """I6 as a predicate. ``capacity < 0`` can never be honoured."""
    if capacity < 0:
        return False
    return overlap_count(intervals) <= capacity


__all__ = [
    "CHARGER_INDEX_PREFIX",
    "CREW_INDEX_PREFIX",
    "PAD_INDEX_PREFIX",
    "UnitResource",
    "canonical_keys",
    "capacity_is_honoured",
    "charger_resources",
    "claim_windows",
    "crew_resources",
    "overlap_count",
    "pad_resources",
    "unit_resources",
    "windows_of",
]
