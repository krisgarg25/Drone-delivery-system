"""Invariant I6 as data: the capacity audit.

``technical-build/06`` section 3 states the property as
``max(overlap_count(commitments)) <= capacity``. This module computes exactly
that expression over committed and offered claims and returns it as a record a
report can print, so the invariant is checked against the same arithmetic the
property test uses rather than against a second, friendlier implementation.

Two failures are distinguished because they need different fixes:

* **facility overbooking** - more simultaneous claims than the site declares
  units for. This is the I6 breach.
* **unit double-booking** - two claims overlapping on one physical pad. This is
  the same breach seen from the object that would actually be collided with.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from .model import ClaimPurpose, ResourceClaim, group_claims
from .resources import claim_windows, overlap_count

UNIT_CAPACITY: int = 1
"""Every facility unit is single-occupancy; the site's capacity is its unit count."""


@dataclass(frozen=True)
class PurposeAudit:
    """One resource kind measured against its declared count."""

    purpose: ClaimPurpose
    capacity: int
    units_in_use: int
    peak_overlap: int
    double_booked_identities: tuple[str, ...]

    @property
    def violated(self) -> bool:
        return self.peak_overlap > self.capacity or bool(self.double_booked_identities)

    def as_metrics(self) -> dict[str, str | int]:
        flat: dict[str, str | int] = {
            "capacity": self.capacity,
            "peak_overlap": self.peak_overlap,
            "units_in_use": self.units_in_use,
            "double_booked_identities": len(self.double_booked_identities),
            "violated": int(self.violated),
        }
        return {f"{self.purpose.value}_{key}": value for key, value in sorted(flat.items())}


@dataclass(frozen=True)
class CapacityAudit:
    """Every purpose at one facility, measured."""

    facility_id: str
    pads: PurposeAudit
    chargers: PurposeAudit
    crew: PurposeAudit

    @property
    def violated(self) -> bool:
        return self.pads.violated or self.chargers.violated or self.crew.violated

    @property
    def peak_pad_overlap(self) -> int:
        return self.pads.peak_overlap

    @property
    def i6_holds(self) -> bool:
        """I6 stated for every resource kind this package schedules."""
        return not self.violated

    def as_metrics(self) -> dict[str, str | int]:
        metrics: dict[str, str | int] = {"facility_id": self.facility_id, "i6_holds": int(self.i6_holds)}
        for audit in (self.pads, self.chargers, self.crew):
            metrics.update(audit.as_metrics())
        return {key: metrics[key] for key in sorted(metrics)}


def audit_purpose(
    claims: Iterable[ResourceClaim], purpose: ClaimPurpose, capacity: int
) -> PurposeAudit:
    """Measure one purpose: peak simultaneity and any unit holding two claims."""
    selected = tuple(claim for claim in claims if claim.purpose is purpose)
    double_booked: list[str] = []
    for identity, group in group_claims(selected):
        if overlap_count(claim_windows(group)) > UNIT_CAPACITY:
            double_booked.append(identity)
    return PurposeAudit(
        purpose=purpose,
        capacity=capacity,
        units_in_use=len({claim.identity_token for claim in selected}),
        peak_overlap=overlap_count(claim_windows(selected)),
        double_booked_identities=tuple(sorted(double_booked)),
    )


def audit_claims(
    claims: Iterable[ResourceClaim],
    *,
    facility_id: str,
    pad_capacity: int,
    charger_capacity: int,
    crew_capacity: int,
) -> CapacityAudit:
    """Audit a set of claims against a facility's declared capacity."""
    every = tuple(claims)
    return CapacityAudit(
        facility_id=facility_id,
        pads=audit_purpose(every, ClaimPurpose.PAD, pad_capacity),
        chargers=audit_purpose(every, ClaimPurpose.CHARGER, charger_capacity),
        crew=audit_purpose(every, ClaimPurpose.CREW, crew_capacity),
    )


__all__ = ["UNIT_CAPACITY", "CapacityAudit", "PurposeAudit", "audit_claims", "audit_purpose"]
