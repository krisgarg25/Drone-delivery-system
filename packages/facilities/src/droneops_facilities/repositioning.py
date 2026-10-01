"""Alternate-site and repositioning *data*: what a dispatcher can price.

When a site cannot serve an arrival, the question shifts to whether another site
could. Deciding that is the dispatcher's job (``conventions.md`` section 7: this
package owns pads, chargers and crew windows, not assignment or priority), so
this module stops at facts a pricing function can consume:

* how many units at the candidate site are free at the instant in question;
* when its crew is next on duty and when it closes;
* the wait the arrival already carries at its current site;
* whether its policy version and handover type are comparable at all.

Notably absent is any distance, duration or cost of flying there. A repositioning
leg belongs to the corridor graph and the energy model; inventing a number here
would be a decision smuggled in as data.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from droneops_contracts.facilities import HandoverType
from droneops_geom_core import Interval

from .model import CapacityProfile, WaitReason
from .resources import UnitResource

PRICED_BY: Final[str] = "dispatcher"
DECIDES_NOTHING: Final[bool] = True
LEG_AUTHORITY: Final[str] = "corridor_graph_and_energy"


@dataclass(frozen=True)
class AvailabilityCounts:
    """Units of a site that are free at one instant."""

    at_us: int
    pads_free: int
    chargers_free: int
    crew_free: bool

    @property
    def can_start_something(self) -> bool:
        return self.pads_free > 0

    def as_metrics(self) -> dict[str, int | bool]:
        return {
            "at_us": self.at_us,
            "pads_free": self.pads_free,
            "chargers_free": self.chargers_free,
            "crew_free": self.crew_free,
        }


@dataclass(frozen=True)
class AlternateSiteQuote:
    """One candidate site, described but not recommended."""

    request_id: str
    origin_facility_id: str
    target_facility_id: str
    reason: WaitReason
    origin_wait_us: int
    target: AvailabilityCounts
    turnaround_us: int
    loading_us: int
    opens_us: int
    closes_us: int
    next_crew_open_us: int
    policy_version: str
    handover_type: HandoverType
    excluded_reason: str | None = None
    priced_by: str = PRICED_BY
    decides_nothing: bool = DECIDES_NOTHING
    leg_authority: str = LEG_AUTHORITY

    @property
    def comparable(self) -> bool:
        """False when the site cannot legally or temporally take this work."""
        return self.excluded_reason is None

    def as_metrics(self) -> dict[str, str | int | bool]:
        metrics: dict[str, str | int | bool] = {
            "request_id": self.request_id,
            "origin_facility_id": self.origin_facility_id,
            "target_facility_id": self.target_facility_id,
            "reason": self.reason.value,
            "origin_wait_us": self.origin_wait_us,
            "comparable": self.comparable,
            "target_turnaround_us": self.turnaround_us,
            "target_next_crew_open_us": self.next_crew_open_us,
            "target_policy_version": self.policy_version,
            "target_handover_type": self.handover_type.value,
            "priced_by": self.priced_by,
            "leg_authority": self.leg_authority,
        }
        metrics.update({f"target_{key}": value for key, value in self.target.as_metrics().items()})
        if self.excluded_reason is not None:
            metrics["excluded_reason"] = self.excluded_reason
        return {key: metrics[key] for key in sorted(metrics)}


OccupiedReader = Callable[[str], "tuple[Interval, ...]"]


def _free_at(units: tuple[UnitResource, ...], occupied: OccupiedReader, at_us: int) -> int:
    instant = Interval.of(at_us, at_us + 1)
    return sum(1 for unit in units if not any(held.overlaps(instant) for held in occupied(unit.identity_token())))


def availability_at(
    profile: CapacityProfile, occupied: OccupiedReader, at_us: int
) -> AvailabilityCounts:
    """How much of a site is idle at ``at_us``, counting only declared units."""
    crew_open = any(shift.contains_instant(at_us) for shift in profile.crew_shifts)
    return AvailabilityCounts(
        at_us=at_us,
        pads_free=_free_at(profile.pads(), occupied, at_us),
        chargers_free=_free_at(profile.chargers(), occupied, at_us),
        crew_free=crew_open and _free_at(profile.crews(), occupied, at_us) > 0,
    )


def next_crew_open_us(profile: CapacityProfile, at_us: int) -> int:
    """The first duty-window start at or after ``at_us``.

    A site with no declared shifts is treated as never staffed rather than as
    always staffed: an absent pilot must never be inferred from missing data.
    """
    starts = [shift.start for shift in profile.crew_shifts if shift.end > at_us]
    if not starts:
        return profile.closes_us
    for shift in profile.crew_shifts:
        if shift.contains_instant(at_us):
            return at_us
    return min(starts)


def quote_for(
    request_id: str,
    origin: CapacityProfile,
    target: CapacityProfile,
    reason: WaitReason,
    occupied: OccupiedReader,
    *,
    at_us: int,
    origin_wait_us: int = 0,
) -> AlternateSiteQuote:
    """Build one quote, marking incomparable sites instead of hiding them."""
    excluded: str | None = None
    if target.policy_version != origin.policy_version:
        excluded = "policy_version"
    elif target.pad_count == 0:
        # A handover-type mismatch cannot appear here: Accessibility rejects
        # anything but land transfer at construction time.
        excluded = "no_pads"
    return AlternateSiteQuote(
        request_id=request_id,
        origin_facility_id=origin.facility_id,
        target_facility_id=target.facility_id,
        reason=reason,
        origin_wait_us=origin_wait_us,
        target=availability_at(target, occupied, at_us),
        turnaround_us=target.turnaround_us,
        loading_us=target.loading_us,
        opens_us=target.opens_us,
        closes_us=target.closes_us,
        next_crew_open_us=next_crew_open_us(target, at_us),
        policy_version=target.policy_version,
        handover_type=target.accessibility.handover_type,
        excluded_reason=excluded,
    )


def alternate_site_quotes(
    request_id: str,
    origin: CapacityProfile,
    targets: tuple[CapacityProfile, ...],
    reason: WaitReason,
    occupied: OccupiedReader,
    *,
    at_us: int,
    origin_wait_us: int = 0,
) -> tuple[AlternateSiteQuote, ...]:
    """Quotes for every other site, in facility-id order. No ranking, no choice."""
    return tuple(
        quote_for(
            request_id,
            origin,
            target,
            reason,
            occupied,
            at_us=at_us,
            origin_wait_us=origin_wait_us,
        )
        for target in sorted(targets, key=lambda item: item.facility_id)
        if target.facility_id != origin.facility_id
    )


__all__ = [
    "DECIDES_NOTHING",
    "LEG_AUTHORITY",
    "PRICED_BY",
    "AlternateSiteQuote",
    "AvailabilityCounts",
    "alternate_site_quotes",
    "availability_at",
    "next_crew_open_us",
    "quote_for",
]
