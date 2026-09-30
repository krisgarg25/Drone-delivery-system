"""The restriction record: what is restricted, where, up to what height, and since when.

A restriction is *data about a claim*, never the claim itself. Six kinds are modelled
because those are the six the architecture names; each carries an effect class that says
what it does to a route, an altitude band that must state its reference, a validity
window, and provenance that cannot be omitted.

Rules encoded here:

* ``provenance`` is a required field: a restriction nobody sourced does not exist.
* ``authoritative`` defaults to false and cannot be set true while the authoritative
  source registry is empty, which it is in Phase 0. ``SyntheticMixin`` from
  ``droneops_contracts`` supplies the field so it cannot be "forgotten" at a call site.
* An altitude band without a reference is rejected (invariant I9), and an open band is
  written as ``ceiling_m=None`` ("no published top"), not as a made-up number.
* ``effective_until=None`` means "until further notice", which is the *least* trustworthy
  window of all: it can only be closed by fresher data, so it ages out through
  :mod:`droneops_airspace.validity` rather than being trusted forever.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import Field, model_validator

from droneops_contracts.geometry import AltitudeReference
from droneops_contracts.provenance import (
    ContractModel,
    Provenance,
    SyntheticMixin,
    UtcDatetime,
)
from droneops_geom_core import (
    AltitudeBand,
    GeoPoint,
    Interval,
    metres_to_millimetres,
    reference_from_token,
)

from .errors import RestrictionInputError
from .geometry import Footprint, FootprintShape

NOT_A_CLEARANCE = (
    "Simulation model only: this is not a flight authorisation, clearance, permission, "
    "NOTAM or zone notification, and resembling a published zone class does not make it "
    "Indian legal alignment."
)

UNPUBLISHED_CEILING_M = 30_000.0
"""Standing in for "no published top": above any drone ceiling, never a claimed limit."""

AUTHORITATIVE_SOURCES: frozenset[str] = frozenset()
"""Registry of feeds an operator has cleared as authoritative. Deliberately empty.

Populating it is an operator decision made outside this package, with credentials and
licence terms attached (technical-build/01 section 16). While it is empty every
restriction here is synthetic by construction, which is the honest Phase 0 state.
"""


class RestrictionKind(StrEnum):
    """The six restriction kinds the airspace package owns."""

    RED = "red"
    YELLOW = "yellow"
    NOTIFIED = "notified"
    AIRPORT_VICINITY = "airport_vicinity"
    TEMPORARY_CLOSURE = "temporary_closure"
    FACILITY_OUTAGE = "facility_outage"


class RestrictionEffect(StrEnum):
    """What a kind does, as a class rather than as an inline branch.

    ``PROHIBIT`` cannot be unlocked by any condition: a red zone is not a paperwork
    problem in this model. ``CONDITIONAL`` needs a named record. ``RESOURCE`` restricts
    a facility rather than the volume.
    """

    PROHIBIT = "prohibit"
    CONDITIONAL = "conditional"
    RESOURCE = "resource"


EFFECT_BY_KIND: dict[RestrictionKind, RestrictionEffect] = {
    RestrictionKind.RED: RestrictionEffect.PROHIBIT,
    RestrictionKind.TEMPORARY_CLOSURE: RestrictionEffect.PROHIBIT,
    RestrictionKind.YELLOW: RestrictionEffect.CONDITIONAL,
    RestrictionKind.NOTIFIED: RestrictionEffect.CONDITIONAL,
    RestrictionKind.AIRPORT_VICINITY: RestrictionEffect.CONDITIONAL,
    RestrictionKind.FACILITY_OUTAGE: RestrictionEffect.RESOURCE,
}

CONDITION_BY_KIND: dict[RestrictionKind, str] = {
    RestrictionKind.YELLOW: "yellow_coordination_recorded",
    RestrictionKind.NOTIFIED: "notam_acknowledged",
    RestrictionKind.AIRPORT_VICINITY: "airport_coordination_recorded",
    RestrictionKind.FACILITY_OUTAGE: "alternate_facility_recorded",
}
"""Condition tokens. A prohibition has no token: nothing recorded here opens a red zone."""


class GeoCoord(ContractModel):
    """A geodetic vertex in degrees. The CRS is EPSG:4326 by construction."""

    lat: float = Field(ge=-90.0, le=90.0)
    lon: float = Field(ge=-180.0, le=180.0)


class FootprintSpec(ContractModel):
    """Serialisable footprint shape: the wire form of :class:`Footprint`."""

    shape: FootprintShape
    points: tuple[GeoCoord, ...] = Field(min_length=1)
    radius_m: int | None = Field(default=None, gt=0)
    width_m: int | None = Field(default=None, gt=0)

    def to_footprint(self) -> Footprint:
        """Build the exact-integer geometry, mapping defects to our own error type."""

        vertices = tuple(GeoPoint(p.lat, p.lon) for p in self.points)
        try:
            return Footprint(
                shape=self.shape,
                vertices=vertices,
                radius_mm=None if self.radius_m is None else metres_to_millimetres(self.radius_m),
                width_mm=None if self.width_m is None else metres_to_millimetres(self.width_m),
            )
        except RestrictionInputError:  # pragma: no cover - defensive
            raise
        except Exception as error:
            raise RestrictionInputError(f"unusable restriction footprint: {error}") from error


class AltitudeLimit(ContractModel):
    """A vertical band that always carries its reference (invariant I9)."""

    floor_m: float = Field(ge=0.0)
    ceiling_m: float | None = Field(default=None, gt=0.0)
    altitude_reference: AltitudeReference

    @model_validator(mode="after")
    def _check(self) -> AltitudeLimit:
        if self.altitude_reference is AltitudeReference.UNSPECIFIED:
            raise ValueError("altitude_reference must be explicit on a restriction band (I9)")
        if self.ceiling_m is not None and self.ceiling_m <= self.floor_m:
            raise ValueError("restriction ceiling must sit above its floor")
        return self

    @property
    def band(self) -> AltitudeBand:
        """The ``geom-core`` band this limit stands for. Nothing re-implements the algebra."""

        ceiling = self.ceiling_m if self.ceiling_m is not None else UNPUBLISHED_CEILING_M
        # The token is re-read by the kernel rather than passed through: two enums that
        # share member names are still different objects, and mixing them silently would
        # turn every band comparison into a reference error.
        return AltitudeBand.from_metres(
            self.floor_m, ceiling, reference_from_token(self.altitude_reference.value)
        )

    @property
    def is_open_ended(self) -> bool:
        return self.ceiling_m is None


class Restriction(ContractModel, SyntheticMixin):
    """One restriction claim: geometry, band, window and provenance, frozen."""

    restriction_id: str = Field(min_length=3)
    kind: RestrictionKind
    footprint: FootprintSpec
    altitude: AltitudeLimit
    effective_from: UtcDatetime
    effective_until: UtcDatetime | None = None
    provenance: Provenance
    label: str = ""
    note: str = NOT_A_CLEARANCE

    @model_validator(mode="after")
    def _check(self) -> Restriction:
        if self.effective_until is not None and self.effective_until <= self.effective_from:
            raise ValueError("restriction window is empty or reversed")
        if self.authoritative and self.provenance.source not in AUTHORITATIVE_SOURCES:
            raise ValueError(
                "authoritative=True requires a source registered as authoritative; the "
                "registry is empty in Phase 0, so no restriction may claim authority"
            )
        return self

    @property
    def effect(self) -> RestrictionEffect:
        return EFFECT_BY_KIND[self.kind]

    @property
    def condition(self) -> str | None:
        """The token that unlocks this kind, or ``None`` for an absolute prohibition."""

        return CONDITION_BY_KIND.get(self.kind)

    @property
    def geometry(self) -> Footprint:
        return self.footprint.to_footprint()

    @property
    def band(self) -> AltitudeBand:
        return self.altitude.band

    def window(self, horizon_end: UtcDatetime) -> Interval:
        """The claim's occupancy interval, clamped to an evaluation horizon.

        An open-ended window does not become "never ends": it is bounded by the
        caller's horizon, so a future query can still see it as active while this
        record stays finite and comparable.
        """

        if horizon_end <= self.effective_from:
            raise RestrictionInputError("evaluation horizon ends before the restriction begins")
        end = self.effective_until if self.effective_until is not None else horizon_end
        end = min(end, horizon_end)
        return Interval.from_datetimes(self.effective_from, end)

    def applies_at(self, moment: datetime) -> bool:
        """Pure window membership, ignoring trust: a *fact* about the record only.

        Validity (does this constrain an operation) is the question
        :func:`droneops_airspace.validity.evaluate_restriction` answers, because it also
        needs freshness.
        """

        if moment < self.effective_from:
            return False
        return self.effective_until is None or moment < self.effective_until

    def describe(self) -> str:
        return (
            f"{self.kind.value} {self.restriction_id} over "
            f"{self.footprint.to_footprint().describe()} [{self.altitude.band}] "
            f"authoritative={str(self.authoritative).lower()}"
        )
