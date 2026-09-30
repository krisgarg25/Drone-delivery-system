"""A snapshot of restrictions, and what may be concluded from it as a whole.

Per-restriction validity lives in :mod:`droneops_airspace.validity`. This module
answers the *collection* question, which is the dangerous one: "the snapshot has no
red zone over there, so may I launch?"

Only if the snapshot is allowed to speak. Three availabilities exist because three
different situations do:

``authoritative_feed``
    Data from a feed an operator registered as authoritative, under authorised
    credentials. Phase 0 has none, and the type system makes that claim unconstructible
    here rather than trusting a flag.
``manual_operator_file``
    A person supplied a restriction file. The restrictions in it constrain operations
    exactly as an authored zone would; the *absence* of a restriction is only as strong
    as the file's freshness, which is why it is checked the same way.
``no_feed_configured``
    Nothing was supplied. This is the default, it is the honest state of the repository,
    and it must never be read as "clear airspace": assessing it yields ``data_usable =
    False`` so every eligibility question fails closed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import NoReturn

from pydantic import Field, model_validator

from droneops_contracts.provenance import (
    ContractModel,
    Freshness,
    Provenance,
    SyntheticMixin,
    UtcDatetime,
    ensure_utc,
)

from .errors import NotAFlightAuthorisationError
from .restriction import AUTHORITATIVE_SOURCES, NOT_A_CLEARANCE, Restriction
from .validity import TRUST_USABLE, DataTrust, FreshnessPolicy, TrustAssessment, assess_trust

NO_FEED_SOURCE = "no-authorised-feed-configured"
NO_FEED_NOTE = (
    "No authorised restriction feed is configured and no manual snapshot was supplied. "
    "An empty set of restrictions here records ignorance, not clear airspace: every "
    "eligibility question against this snapshot must fail closed."
)


class SnapshotAvailability(StrEnum):
    AUTHORITATIVE_FEED = "authoritative_feed"
    MANUAL_OPERATOR_FILE = "manual_operator_file"
    NO_FEED_CONFIGURED = "no_feed_configured"


class SnapshotAssessment(ContractModel):
    """Whether the collection may be consulted at all, with the reason attached."""

    data_usable: bool
    trust: DataTrust
    reasons: tuple[str, ...]
    restriction_count: int
    availability: SnapshotAvailability
    authoritative: bool = False
    note: str = NOT_A_CLEARANCE

    @property
    def launch_permitted(self) -> bool:
        return self.data_usable

    def as_flight_authorisation(self) -> NoReturn:
        raise NotAFlightAuthorisationError(
            "a restriction snapshot assessment is simulation output and cannot be "
            "read as a flight authorisation"
        )


class RestrictionSnapshot(ContractModel, SyntheticMixin):
    """Normalised restriction observations, with source, fetch time and version."""

    snapshot_id: str = Field(min_length=3)
    schema_version: int = 1
    source: str
    fetched_at: UtcDatetime
    feed_version: str = ""
    availability: SnapshotAvailability
    restrictions: tuple[Restriction, ...] = ()
    content_sha256: str = ""
    note: str = NOT_A_CLEARANCE

    @model_validator(mode="after")
    def _check(self) -> RestrictionSnapshot:
        ids = [restriction.restriction_id for restriction in self.restrictions]
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        if duplicates:
            raise ValueError(f"duplicate restriction ids in snapshot: {duplicates}")
        if self.availability is SnapshotAvailability.AUTHORITATIVE_FEED:
            if not self.authoritative:
                raise ValueError("an authoritative feed snapshot must say it is authoritative")
            if self.source not in AUTHORITATIVE_SOURCES:
                raise ValueError(
                    "no source is registered as authoritative in Phase 0, so an "
                    "authoritative-feed snapshot cannot be constructed"
                )
        if self.availability is SnapshotAvailability.MANUAL_OPERATOR_FILE and self.authoritative:
            raise ValueError(
                "a manually supplied file is not an authoritative feed; hand it to a "
                "safety reviewer instead of setting a flag"
            )
        if self.availability is SnapshotAvailability.NO_FEED_CONFIGURED and self.restrictions:
            raise ValueError(
                "a no-feed snapshot carrying restrictions is a contradiction: label its "
                "real source instead of hiding the data behind a default"
            )
        return self

    @classmethod
    def no_feed(cls, now: UtcDatetime | None = None) -> RestrictionSnapshot:
        """The default observation set: nothing, honestly recorded as nothing."""

        moment = now or datetime.now(UTC)
        ensure_utc(moment)
        return cls(
            snapshot_id="airspace-no-feed",
            source=NO_FEED_SOURCE,
            fetched_at=moment,
            feed_version="none",
            availability=SnapshotAvailability.NO_FEED_CONFIGURED,
            restrictions=(),
            note=NO_FEED_NOTE,
        )

    def ids(self) -> tuple[str, ...]:
        return tuple(restriction.restriction_id for restriction in self.restrictions)

    def assess(self, now: UtcDatetime, policy: FreshnessPolicy | None = None) -> SnapshotAssessment:
        """Can this snapshot be consulted? Stale or absent data answers 'no'."""

        active_policy = policy or FreshnessPolicy()
        if self.availability is SnapshotAvailability.NO_FEED_CONFIGURED:
            return SnapshotAssessment(
                data_usable=False,
                trust=DataTrust.UNUSABLE,
                reasons=(NO_FEED_NOTE,),
                restriction_count=len(self.restrictions),
                availability=self.availability,
                authoritative=self.authoritative,
                note=self.note,
            )
        trust: TrustAssessment = assess_trust(self._provenance_view(), now, active_policy)
        usable = trust.trust.value in TRUST_USABLE
        reasons = list(trust.reasons)
        if not usable:
            reasons.append(
                f"{len(self.restrictions)} restriction(s) are listed, but a stale snapshot "
                "cannot show what was added after it was fetched, so the whole set is unusable"
            )
        return SnapshotAssessment(
            data_usable=usable,
            trust=trust.trust,
            reasons=tuple(reasons),
            restriction_count=len(self.restrictions),
            availability=self.availability,
            authoritative=self.authoritative,
            note=self.note,
        )

    def _provenance_view(self) -> Provenance:
        """The snapshot's own source/fetch pair, as a shared contract record.

        ``freshness`` is declared ``FRESH`` here because ``fetched_at`` is this
        snapshot's own observation moment: its age is computed against ``now`` by the
        policy, not asserted by a file. Restriction-level provenance keeps whatever the
        source declared, which is where an honesty failure shows up.
        """

        return Provenance(
            source=self.source,
            retrieved_at=self.fetched_at,
            expires_at=None,
            freshness=Freshness.FRESH,
        )
