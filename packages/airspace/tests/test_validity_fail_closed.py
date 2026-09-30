"""Validity: in force AND fresh, and every failure mode is a no-launch.

The spec requirement under test is one sentence from ``technical-build/01`` section 16
("fail conservatively when data is stale") read together with invariant I3. It is checked
from both directions, because a single-direction test passes for the wrong reason:

* fresh + in force  -> the restriction applies (it is not ignored);
* fresh + ended     -> the restriction genuinely does not apply (it is not a permanent wall);
* stale / expired   -> no launch, *including* when the record says the window has ended;
* unknown / unsourced, or clock skew -> no launch.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from airspace_support import (
    NOW,
    circle_footprint,
    policy,
    provenance,
    restriction,
    untrusted_restriction,
)
from pydantic import ValidationError

from droneops_airspace import (
    DataTrust,
    NotAFlightAuthorisationError,
    RestrictionKind,
    WindowState,
    assess_trust,
    evaluate_restriction,
)
from droneops_contracts.geometry import AltitudeReference
from droneops_contracts.provenance import Freshness

pytestmark = pytest.mark.unit


def test_in_force_and_fresh_applies() -> None:
    validity = evaluate_restriction(restriction(), NOW, policy())
    assert validity.window is WindowState.ACTIVE
    assert validity.trust is DataTrust.TRUSTED
    assert validity.applies is True
    assert validity.fail_closed is False
    assert validity.launch_permitted is True


def test_in_force_but_stale_fails_closed_instead_of_being_ignored() -> None:
    """The exact inversion: a stale record must never read as 'no restriction'."""

    stale = untrusted_restriction()
    validity = evaluate_restriction(stale, NOW, policy())
    assert validity.applies is False
    assert validity.fail_closed is True
    assert validity.launch_permitted is False
    assert any("no launch" in reason.replace("-", " ") for reason in validity.reasons)


def test_ended_window_with_fresh_data_is_genuinely_not_in_force() -> None:
    """The other direction: trustworthy data must be able to say a zone lifted."""

    closed = restriction(effective_until=NOW - timedelta(minutes=5))
    validity = evaluate_restriction(closed, NOW, policy())
    assert validity.window is WindowState.ENDED
    assert validity.trust is DataTrust.TRUSTED
    assert validity.applies is False
    assert validity.fail_closed is False
    assert validity.launch_permitted is True


def test_ended_window_with_stale_data_is_unknown_not_lifted() -> None:
    closed = untrusted_restriction(effective_until=NOW - timedelta(minutes=5))
    validity = evaluate_restriction(closed, NOW, policy())
    assert validity.window is WindowState.ENDED
    assert validity.fail_closed is True
    assert any("not fresh enough" in reason for reason in validity.reasons)


def test_future_window_is_not_in_force_yet_but_is_trusted() -> None:
    upcoming = restriction(
        effective_from=NOW + timedelta(hours=2), effective_until=NOW + timedelta(hours=3)
    )
    validity = evaluate_restriction(upcoming, NOW, policy())
    assert validity.window is WindowState.NOT_YET
    assert validity.applies is False
    assert validity.fail_closed is False


def test_expired_provenance_is_a_distinct_trust_state() -> None:
    expired = restriction(
        source_provenance=provenance(
            retrieved_at=NOW - timedelta(hours=2),
            expires_at=NOW - timedelta(minutes=1),
            freshness=Freshness.FRESH,
        )
    )
    validity = evaluate_restriction(expired, NOW, policy())
    assert validity.trust is DataTrust.EXPIRED_PROVENANCE
    assert validity.fail_closed is True
    # A record whose own source disowned it cannot be rescued by a declared "fresh".
    assert validity.declared_freshness is Freshness.FRESH


def test_source_less_record_is_unusable() -> None:
    anonymous = restriction(source_provenance=provenance(source="   "))
    validity = evaluate_restriction(anonymous, NOW, policy())
    assert validity.trust is DataTrust.UNUSABLE
    assert validity.fail_closed is True


def test_future_retrieved_at_is_clock_skew_not_freshness() -> None:
    skewed = restriction(source_provenance=provenance(retrieved_at=NOW + timedelta(minutes=30)))
    assessment = assess_trust(skewed.provenance, NOW, policy())
    assert assessment.trust is DataTrust.UNUSABLE
    assert any("clock skew" in reason for reason in assessment.reasons)


def test_undeclared_freshness_is_treated_as_stale() -> None:
    unknown = restriction(
        source_provenance=provenance(
            retrieved_at=NOW - timedelta(minutes=5), freshness=Freshness.UNKNOWN
        )
    )
    assert evaluate_restriction(unknown, NOW, policy()).trust is DataTrust.STALE
    permissive = policy(treat_unknown_freshness_as_stale=False)
    assert evaluate_restriction(unknown, NOW, permissive).trust is DataTrust.TRUSTED


def test_age_grading_trusted_aging_stale() -> None:
    strict = policy(max_trusted_age=timedelta(hours=1), aging_grace=timedelta(hours=1))
    for age, expected in (
        (timedelta(minutes=30), DataTrust.TRUSTED),
        (timedelta(hours=1, minutes=30), DataTrust.AGING),
        (timedelta(hours=3), DataTrust.STALE),
    ):
        candidate = restriction(source_provenance=provenance(retrieved_at=NOW - age))
        assert assess_trust(candidate.provenance, NOW, strict).trust is expected


def test_open_ended_window_still_ages_out() -> None:
    """'Until further notice' is the least trustworthy window there is."""

    forever = restriction(
        effective_until=None,
        source_provenance=provenance(retrieved_at=NOW - timedelta(days=9)),
    )
    validity = evaluate_restriction(forever, NOW, policy())
    assert validity.window is WindowState.ACTIVE
    assert validity.applies is False
    assert validity.fail_closed is True


def test_every_kind_is_modelled() -> None:
    assert {kind.value for kind in RestrictionKind} == {
        "red",
        "yellow",
        "notified",
        "airport_vicinity",
        "temporary_closure",
        "facility_outage",
    }


def test_altitude_reference_is_mandatory_on_a_band() -> None:
    with pytest.raises(ValidationError, match="I9"):
        restriction(altitude_reference=AltitudeReference.UNSPECIFIED)


def test_synthetic_restriction_cannot_claim_authority() -> None:
    assert restriction().authoritative is False
    with pytest.raises(ValidationError, match="authoritative"):
        restriction(authoritative=True)


def test_reversed_window_is_rejected() -> None:
    with pytest.raises(ValidationError, match="empty or reversed"):
        restriction(effective_from=NOW, effective_until=NOW - timedelta(hours=1))


def test_validity_refuses_to_be_read_as_clearance() -> None:
    validity = evaluate_restriction(restriction(), NOW, policy())
    with pytest.raises(NotAFlightAuthorisationError):
        validity.as_flight_authorisation()


def test_airport_buffer_is_a_circle_on_the_real_osm_point_with_an_open_band() -> None:
    zone = restriction(
        restriction_id="apt-1",
        kind=RestrictionKind.AIRPORT_VICINITY,
        footprint=circle_footprint(radius_m=3000),
        ceiling_m=None,
    )
    assert zone.altitude.is_open_ended is True
    assert zone.band.upper_mm > 1_000_000
    assert zone.effect.value == "conditional"
