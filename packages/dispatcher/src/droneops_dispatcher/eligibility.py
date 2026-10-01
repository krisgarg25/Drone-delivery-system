"""Invariant I5: the candidate *query* excludes non-operable aircraft.

``technical-build/06`` section 2 says the test must not merely assert that an
aircraft is "not available" - it must assert that the candidate query omits it.
That is only observable if exclusion happens here, once, and the ranked list is
built from the surviving rows. So this module does not score aircraft at all: it
answers the narrower question "may this tail even be offered a new commitment?"
and returns the answer as two disjoint collections plus an explanation for each
excluded tail. A caller that wanted to rank an offline aircraft last instead of
excluding it would have to reach past :meth:`EligibilityQuery.candidate_ids`, and
:meth:`EligibilityQuery.assert_candidates_are` makes that a loud error.

Why the dispatcher's set is narrower than the ledger's
    ``droneops_reservations.intake.OPERABLE_AIRCRAFT`` is the *floor*: the statuses
    a commitment may exist on. This layer adds the operational judgement that
    precedes it - an aircraft already ``reserved``, ``flying``, ``loading`` or
    ``unloading`` is operable but not free for a new mission in this round, and a
    stale or lost observation makes its position uncertain (invariant I3), which is
    a no-launch condition. Widening beyond the ledger's floor is refused at
    construction, so this query can never admit something the ledger will reject.

Connectivity is orthogonal to availability (``conventions.md`` section 9): an
aircraft can be online and still ineligible, and offline is a state, not a link
quality. Both are reported separately so the explanation is honest about which.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from droneops_contracts import AircraftStatus, LinkQuality

from .errors import EligibilityQueryError
from .policy import MICROS_PER_HOUR, DispatchPolicy, digest_of

#: Statuses this query will offer for a new mission in this round.
ASSIGNABLE_STATES: frozenset[AircraftStatus] = frozenset(
    {AircraftStatus.AVAILABLE, AircraftStatus.CHARGING}
)

#: Statuses that can never be assigned new work, whatever the mission.
NEVER_ASSIGNABLE: frozenset[AircraftStatus] = frozenset(
    {
        AircraftStatus.OFFLINE,
        AircraftStatus.MAINTENANCE,
        AircraftStatus.RETIRED,
        AircraftStatus.CONTINGENCY,
    }
)

#: Statuses that are operable but busy: excluded from *this* round only.
BUSY_STATES: frozenset[AircraftStatus] = frozenset(
    {
        AircraftStatus.RESERVED,
        AircraftStatus.FLYING,
        AircraftStatus.LOADING,
        AircraftStatus.UNLOADING,
        AircraftStatus.REGISTERED,
    }
)

RELIABLE_LINKS: frozenset[LinkQuality] = frozenset({LinkQuality.GOOD, LinkQuality.DEGRADED})

#: The ledger's floor, restated as data so this package can prove it stays inside it
#: without importing the ledger (``droneops_reservations.intake.OPERABLE_AIRCRAFT``).
#: ``test_i5_eligibility_query.py`` asserts ``ASSIGNABLE_STATES`` is a subset: the
#: dispatcher may narrow the query, never widen it past what can be committed.
OPERABLE_FLOOR: frozenset[AircraftStatus] = frozenset(
    {
        AircraftStatus.AVAILABLE,
        AircraftStatus.RESERVED,
        AircraftStatus.CHARGING,
        AircraftStatus.LOADING,
        AircraftStatus.UNLOADING,
    }
)


class IneligibleReason(StrEnum):
    """Why a tail was not in the candidate query, as a stable code."""

    OFFLINE = "aircraft_offline"
    MAINTENANCE = "aircraft_maintenance"
    RETIRED = "aircraft_retired"
    CONTINGENCY = "aircraft_contingency"
    BUSY_THIS_ROUND = "aircraft_busy_this_round"
    AIRBORNE = "aircraft_airborne"
    LINK_UNCERTAIN = "aircraft_link_uncertain"
    STALE_OBSERVATION = "aircraft_observation_stale"
    PAYLOAD_EXCEEDED = "aircraft_payload_exceeded"
    CHARGER_UNREADY = "aircraft_charger_unready"
    UNKNOWN_STATUS = "aircraft_status_unknown"


_NEVER_READY_US: Final[int] = 1 << 60
"""A sentinel for "no charge power is declared", never zero and never a guess."""


@dataclass(frozen=True)
class FleetAircraft:
    """One tail as the dispatcher sees it: a state, a battery and a base."""

    aircraft_id: str
    status: AircraftStatus
    link: LinkQuality
    profile_id: str
    profile_version: str
    capacity_wh: int
    charge_bp: int
    home_facility_id: str
    cruise_speed_mm_per_s: int
    max_payload_g: int
    last_observation_us: int
    soh_bp: int = 10_000
    busy_until_us: int = 0
    airborne_mission_id: str | None = None

    def __post_init__(self) -> None:
        if not self.aircraft_id:
            raise EligibilityQueryError("a fleet row needs an aircraft id")
        if self.capacity_wh <= 0:
            raise EligibilityQueryError(f"{self.aircraft_id}: usable capacity must be positive")
        if not 0 <= self.charge_bp <= 10_000:
            raise EligibilityQueryError(f"{self.aircraft_id}: charge is in basis points")
        if self.soh_bp <= 0 or self.soh_bp > 10_000:
            raise EligibilityQueryError(f"{self.aircraft_id}: state of health is in basis points")
        if self.busy_until_us < 0:
            raise EligibilityQueryError(f"{self.aircraft_id}: busy-until cannot be negative")

    def is_airborne(self) -> bool:
        return self.status is AircraftStatus.FLYING or self.airborne_mission_id is not None

    @property
    def charge_wh(self) -> int:
        """Energy on board, from the basis-point state the telemetry projection gave."""
        return self.capacity_wh * self.charge_bp // 10_000

    def observation_age_us(self, as_of_us: int) -> int:
        return max(0, as_of_us - self.last_observation_us)

    def ready_after_recharge_us(self, *, charge_power_w: int, full_charge_bp: int = 10_000) -> int:
        """When this tail could roll, counting the charge it still needs.

        A tail on a charger is assignable, but not immediately: the honest number is
        "when the battery reaches ``full_charge_bp`` at the site's declared charge
        power", and that is what the time-window layer hands to the solver. Zero
        declared power is a missing datum, not free charging, so the tail is treated
        as ready never rather than ready now.
        """
        needed_wh = max(0, (self.capacity_wh * full_charge_bp - self.charge_wh * 10_000) // 10_000)
        if needed_wh == 0:
            return self.busy_until_us
        if charge_power_w <= 0:
            return self.busy_until_us + _NEVER_READY_US
        return self.busy_until_us + needed_wh * MICROS_PER_HOUR // charge_power_w


@dataclass(frozen=True)
class ExcludedAircraft:
    """An aircraft the query left out, with the reason it left out."""

    aircraft_id: str
    reason: IneligibleReason
    detail: str
    status: str
    persisted: bool = True

    def as_row(self) -> tuple[str, str, str]:
        return (self.aircraft_id, self.reason.value, self.detail)


@dataclass(frozen=True)
class EligibilityQuery:
    """The candidate query and the exclusions, as two disjoint collections."""

    as_of_us: int
    candidates: tuple[FleetAircraft, ...]
    excluded: tuple[ExcludedAircraft, ...]
    policy_version: str
    query_digest: str

    @property
    def candidate_ids(self) -> frozenset[str]:
        return frozenset(row.aircraft_id for row in self.candidates)

    @property
    def excluded_ids(self) -> frozenset[str]:
        return frozenset(row.aircraft_id for row in self.excluded)

    def assert_candidates_are(self, ids: Sequence[str]) -> None:
        """Fail if anything outside ``ids`` reached the candidate list.

        The I5 test calls this: passing an aircraft in ``ids`` is not enough, an
        implementation that ranked offline tails last would still "not assign" them.
        """
        allowed = set(ids)
        extra = sorted(self.candidate_ids - allowed)
        if extra:
            raise EligibilityQueryError(
                f"candidate query included aircraft it must not offer: {extra}"
            )

    def explanation_of(self, aircraft_id: str) -> ExcludedAircraft | None:
        for row in self.excluded:
            if row.aircraft_id == aircraft_id:
                return row
        return None

    def rows(self) -> tuple[tuple[str, str, str], ...]:
        return tuple(
            sorted((row.aircraft_id, row.reason.value, row.status) for row in self.excluded)
        )


def _why_not(  # noqa: PLR0911 - one explicit refusal per exclusion rule
    aircraft: FleetAircraft,
    *,
    policy: DispatchPolicy,
    as_of_us: int,
    payload_g: int,
    charge_power_w: int | None,
    latest_start_us: int,
) -> ExcludedAircraft | None:
    status = aircraft.status
    if status is AircraftStatus.OFFLINE:
        return _excluded(aircraft, IneligibleReason.OFFLINE, "offline: no link, no authority")
    if status is AircraftStatus.MAINTENANCE:
        return _excluded(
            aircraft,
            IneligibleReason.MAINTENANCE,
            "in maintenance: a maintenance state is not a scheduling delay",
        )
    if status is AircraftStatus.RETIRED:
        return _excluded(aircraft, IneligibleReason.RETIRED, "retired airframe")
    if status is AircraftStatus.CONTINGENCY:
        return _excluded(
            aircraft, IneligibleReason.CONTINGENCY, "in contingency: recovering, not available"
        )
    if status is AircraftStatus.FLYING:
        return _excluded(
            aircraft,
            IneligibleReason.AIRBORNE,
            "airborne: an aircraft already flying holds a commitment",
        )
    if status not in ASSIGNABLE_STATES:
        return _excluded(
            aircraft,
            IneligibleReason.BUSY_THIS_ROUND,
            f"status {status.value} is operable but occupied this round",
        )
    if aircraft.link not in RELIABLE_LINKS:
        return _excluded(
            aircraft,
            IneligibleReason.LINK_UNCERTAIN,
            f"link {aircraft.link.value}: position is uncertain, so no new airspace",
        )
    if aircraft.observation_age_us(as_of_us) > policy.max_observation_age_us:
        return _excluded(
            aircraft,
            IneligibleReason.STALE_OBSERVATION,
            f"observation is {aircraft.observation_age_us(as_of_us)} us old, "
            f"bound {policy.max_observation_age_us} us",
        )
    if payload_g > aircraft.max_payload_g:
        return _excluded(
            aircraft,
            IneligibleReason.PAYLOAD_EXCEEDED,
            f"payload {payload_g} g over certified {aircraft.max_payload_g} g",
        )
    if charge_power_w is not None:
        ready_us = aircraft.ready_after_recharge_us(charge_power_w=charge_power_w)
        if ready_us > latest_start_us:
            return _excluded(
                aircraft,
                IneligibleReason.CHARGER_UNREADY,
                f"ready at {ready_us} us, after the {latest_start_us} us window closes "
                f"(charge {aircraft.charge_bp} bp of {aircraft.capacity_wh} Wh at "
                f"{charge_power_w} W)",
            )
    return None


def _excluded(aircraft: FleetAircraft, reason: IneligibleReason, detail: str) -> ExcludedAircraft:
    return ExcludedAircraft(
        aircraft_id=aircraft.aircraft_id,
        reason=reason,
        detail=detail,
        status=aircraft.status.value,
    )


def candidate_query(
    fleet: Sequence[FleetAircraft],
    *,
    policy: DispatchPolicy,
    as_of_us: int,
    payload_g: int = 0,
    charge_power_w: int | None = None,
    latest_start_us: int | None = None,
) -> EligibilityQuery:
    """Build the candidate list for one mission shape. Exclusion, not demotion.

    ``charge_power_w`` and ``latest_start_us`` are optional because a query with no
    declared charge power cannot honestly answer "ready by when" - and refusing to
    guess is the point. Pass them when the mission has a deadline, so an aircraft
    still on a slow charger is excluded rather than ranked last.
    """
    if as_of_us < 0:
        raise EligibilityQueryError("kernel time cannot be negative")
    window_us = as_of_us if latest_start_us is None else max(as_of_us, latest_start_us)
    candidates: list[FleetAircraft] = []
    excluded: list[ExcludedAircraft] = []
    for aircraft in fleet:
        reason = _why_not(
            aircraft,
            policy=policy,
            as_of_us=as_of_us,
            payload_g=payload_g,
            charge_power_w=charge_power_w,
            latest_start_us=window_us,
        )
        if reason is None:
            candidates.append(aircraft)
        else:
            excluded.append(reason)
    candidates.sort(key=lambda row: row.aircraft_id)
    excluded.sort(key=lambda row: (row.aircraft_id, row.reason.value))
    digest = digest_of(
        [
            ("as_of_us", as_of_us),
            ("payload_g", payload_g),
            ("policy_version", policy.policy_version),
            ("candidates", ",".join(row.aircraft_id for row in candidates)),
            ("excluded", ",".join(f"{row.aircraft_id}:{row.reason.value}" for row in excluded)),
        ]
    )
    return EligibilityQuery(
        as_of_us=as_of_us,
        candidates=tuple(candidates),
        excluded=tuple(excluded),
        policy_version=policy.policy_version,
        query_digest=digest,
    )


def protected_tails(fleet: Sequence[FleetAircraft]) -> tuple[tuple[str, str], ...]:
    """``(aircraft_id, mission_id)`` for every tail holding an airborne commitment.

    The priority layer takes this list and refuses to move anything in it; the
    point of deriving it here is that the same state check that keeps an aircraft
    out of the candidate query is the one that makes its mission untouchable.
    """
    return tuple(
        sorted(
            (
                (row.aircraft_id, str(row.airborne_mission_id or f"mission-of-{row.aircraft_id}"))
                for row in fleet
                if row.is_airborne()
            ),
            key=lambda pair: (pair[1], pair[0]),
        )
    )


__all__ = [
    "ASSIGNABLE_STATES",
    "BUSY_STATES",
    "NEVER_ASSIGNABLE",
    "OPERABLE_FLOOR",
    "RELIABLE_LINKS",
    "EligibilityQuery",
    "ExcludedAircraft",
    "FleetAircraft",
    "IneligibleReason",
    "candidate_query",
    "protected_tails",
]
