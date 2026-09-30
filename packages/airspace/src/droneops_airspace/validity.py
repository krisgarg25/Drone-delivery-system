"""Validity: a restriction applies only when it is in force *and* the data is fresh.

This is the module where the failure model is decided, so the two axes are kept
apart and then combined explicitly.

Axis 1 - the window (a fact about the record): ``not_yet``, ``active``, ``ended``.

Axis 2 - trust in the data (a fact about provenance): ``trusted``, ``aging``,
``stale``, ``expired_provenance``, ``unusable``.

Combination:

===========================  =========================  ==========================
Window                       Trust                      Result
===========================  ==========================  ==========================
active                       trusted / aging            applies
active                       stale / expired / unusable  fail closed: no launch
ended                        trusted / aging            does not apply
ended                        stale / expired / unusable  fail closed: no launch
not_yet                      trusted / aging            applies during its window
any                          unusable                   fail closed: no launch
===========================  ==========================  ==========================

The asymmetry is the whole point. A fresh record may say "this zone is over"; a
stale one may not, because the only thing it actually proves is that nobody told
it to stop. Expired or stale data therefore produces ``fail_closed=True`` - a
no-launch - and never a silent "no restriction". That is the airspace reading of
invariant I3 and of ``technical-build/01`` section 16 ("fail conservatively when
data is stale").

Age is measured against an injected ``now``: nothing here reads a clock, so a
replay of the same inputs gives the same verdict (invariant I7), and a
``retrieved_at`` in the future is rejected as clock skew rather than believed.
"""

from __future__ import annotations

from datetime import timedelta
from enum import StrEnum
from typing import NoReturn

from pydantic import Field

from droneops_contracts.provenance import (
    ContractModel,
    Freshness,
    Provenance,
    UtcDatetime,
    ensure_utc,
)

from .errors import NotAFlightAuthorisationError
from .restriction import NOT_A_CLEARANCE, Restriction, RestrictionEffect, RestrictionKind

TRUST_USABLE = ("trusted", "aging")
"""The two trust states that may support a launch decision at all."""


class WindowState(StrEnum):
    NOT_YET = "not_yet"
    ACTIVE = "active"
    ENDED = "ended"


class DataTrust(StrEnum):
    TRUSTED = "trusted"
    AGING = "aging"
    STALE = "stale"
    EXPIRED_PROVENANCE = "expired_provenance"
    UNUSABLE = "unusable"


STALE_NO_LAUNCH = (
    "stale restriction data: the zone cannot be treated as lifted, so this is a "
    "no-launch decision, not a no-restriction decision"
)
EXPIRED_DATA_NO_LAUNCH = (
    "expired restriction data: the source said this record stops being usable, so the "
    "real situation is unknown and the only safe answer is no launch"
)


class FreshnessPolicy(ContractModel):
    """How long a restriction record may be believed.

    The numbers are experiment parameters read from a versioned config upstream, never
    certified values (conventions section 3), which is why ``illustrative`` is on the
    model itself and ``policy_version`` travels with every verdict.
    """

    policy_version: str = "airspace-freshness-v1"
    max_trusted_age: timedelta = Field(default=timedelta(hours=6), gt=timedelta(0))
    aging_grace: timedelta = Field(default=timedelta(hours=6), ge=timedelta(0))
    treat_unknown_freshness_as_stale: bool = True
    illustrative: bool = True

    @property
    def stale_after(self) -> timedelta:
        return self.max_trusted_age + self.aging_grace


class TrustAssessment(ContractModel):
    trust: DataTrust
    reasons: tuple[str, ...]
    age_s: int | None = None
    declared_freshness: Freshness = Freshness.UNKNOWN


class RestrictionValidity(ContractModel):
    """The verdict for one restriction at one instant. Not a clearance (see note)."""

    restriction_id: str
    kind: RestrictionKind
    effect: RestrictionEffect
    window: WindowState
    trust: DataTrust
    applies: bool
    fail_closed: bool
    reasons: tuple[str, ...]
    age_s: int | None = None
    declared_freshness: Freshness = Freshness.UNKNOWN
    policy_version: str = "airspace-freshness-v1"
    authoritative: bool = False
    note: str = NOT_A_CLEARANCE

    @property
    def data_is_usable(self) -> bool:
        return self.trust.value in TRUST_USABLE

    @property
    def launch_permitted(self) -> bool:
        """Whether this restriction lets an operation proceed on data grounds.

        It says nothing about whether the route intersects the zone: that is the
        eligibility question. A ``False`` here is a refusal to answer, not a "no".
        """

        return self.data_is_usable

    def as_flight_authorisation(self) -> NoReturn:
        """Refuse the one interpretation this type must never receive."""

        raise NotAFlightAuthorisationError(
            f"validity of restriction {self.restriction_id} is a simulation verdict; "
            "it is not, and cannot be converted into, a flight authorisation"
        )


def _declared_verdict(
    provenance: Provenance, age_s: int, policy: FreshnessPolicy
) -> TrustAssessment | None:
    """What the source itself says, which can only ever make a record less believable."""

    if provenance.freshness is Freshness.STALE:
        return TrustAssessment(
            trust=DataTrust.STALE,
            declared_freshness=provenance.freshness,
            age_s=age_s,
            reasons=(STALE_NO_LAUNCH, "the source itself declared this record stale"),
        )
    if provenance.freshness is Freshness.UNKNOWN and policy.treat_unknown_freshness_as_stale:
        return TrustAssessment(
            trust=DataTrust.STALE,
            declared_freshness=provenance.freshness,
            age_s=age_s,
            reasons=(STALE_NO_LAUNCH, "freshness was never declared, and unknown is not fresh"),
        )
    return None


def _age_verdict(provenance: Provenance, age_s: int, policy: FreshnessPolicy) -> TrustAssessment:
    """Grade the record by how old it is at the evaluation instant."""

    max_age = int(policy.max_trusted_age.total_seconds())
    stale_after = int(policy.stale_after.total_seconds())
    trust: DataTrust
    reasons: tuple[str, ...]
    if age_s <= max_age:
        trust, reasons = (
            DataTrust.TRUSTED,
            (f"record is {age_s} s old, within the {max_age} s trusted window",),
        )
    elif age_s <= stale_after:
        trust, reasons = (
            DataTrust.AGING,
            (
                f"record is {age_s} s old, past the trusted window ({max_age} s) but inside the "
                f"grace period ({stale_after} s): usable, and reported as aging",
            ),
        )
    else:
        trust, reasons = (
            DataTrust.STALE,
            (STALE_NO_LAUNCH, f"record is {age_s} s old, past {stale_after} s"),
        )
    return TrustAssessment(
        trust=trust, declared_freshness=provenance.freshness, age_s=age_s, reasons=reasons
    )


def assess_trust(
    provenance: Provenance, now: UtcDatetime, policy: FreshnessPolicy
) -> TrustAssessment:
    """Decide whether a record may be believed, erring toward 'no' in every branch."""

    ensure_utc(now)
    if not provenance.source.strip():
        return TrustAssessment(
            trust=DataTrust.UNUSABLE,
            declared_freshness=provenance.freshness,
            reasons=("a restriction with no source is not evidence of anything",),
        )
    age = now - provenance.retrieved_at
    age_s = int(age.total_seconds())
    if age_s < 0:
        return TrustAssessment(
            trust=DataTrust.UNUSABLE,
            declared_freshness=provenance.freshness,
            reasons=(
                f"retrieved_at is {abs(age_s)} s in the future: clock skew, reject or "
                "quarantine rather than believe the record",
            ),
        )
    if provenance.is_expired(now):
        return TrustAssessment(
            trust=DataTrust.EXPIRED_PROVENANCE,
            declared_freshness=provenance.freshness,
            age_s=age_s,
            reasons=(EXPIRED_DATA_NO_LAUNCH,),
        )
    declared = _declared_verdict(provenance, age_s, policy)
    if declared is not None:
        return declared
    return _age_verdict(provenance, age_s, policy)


def window_state_at(restriction: Restriction, now: UtcDatetime) -> WindowState:
    ensure_utc(now)
    if now < restriction.effective_from:
        return WindowState.NOT_YET
    if restriction.effective_until is not None and now >= restriction.effective_until:
        return WindowState.ENDED
    return WindowState.ACTIVE


def evaluate_restriction(
    restriction: Restriction, now: UtcDatetime, policy: FreshnessPolicy | None = None
) -> RestrictionValidity:
    """The single entry point for "does this restriction apply right now?"."""

    active_policy = policy or FreshnessPolicy()
    trust = assess_trust(restriction.provenance, now, active_policy)
    window = window_state_at(restriction, now)
    usable = trust.trust.value in TRUST_USABLE
    applies = usable and window is WindowState.ACTIVE
    fail_closed = not usable
    reasons = list(trust.reasons)
    if fail_closed and window is WindowState.ENDED:
        reasons.append(
            "the record says this window has ended, but the record is not fresh enough to be "
            "believed: an ended window in stale data is unknown, not lifted"
        )
    if fail_closed and window is WindowState.NOT_YET:
        reasons.append(
            "a future window in stale data is still a window: no launch on the strength of it"
        )
    if applies:
        reasons.append("in force and fresh: this restriction constrains anything it intersects")
    if window is WindowState.NOT_YET and usable:
        reasons.append(
            "not in force at the evaluation instant; it constrains the plan during its window"
        )
    return RestrictionValidity(
        restriction_id=restriction.restriction_id,
        kind=restriction.kind,
        effect=restriction.effect,
        window=window,
        trust=trust.trust,
        applies=applies,
        fail_closed=fail_closed,
        reasons=tuple(reasons),
        age_s=trust.age_s,
        declared_freshness=trust.declared_freshness,
        policy_version=active_policy.policy_version,
        authoritative=restriction.authoritative,
    )
