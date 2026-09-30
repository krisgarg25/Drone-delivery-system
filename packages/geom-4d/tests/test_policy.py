"""Policy inputs are configuration, versioned, separate - and never invented.

What these prove: the four uncertainty inputs and the transition buffer arrive
from the versioned YAML file as five distinct numbers, they are converted to
integer kernel units exactly once, an absent or malformed input is a rejection
rather than a default, and the rounding direction of every derived quantity is
outward.

What these do not prove: that the margins are the right ones. They are
experiment parameters, and their values are not a safety claim (the file says so
itself); only the invariants are.
"""

from __future__ import annotations

import math
from fractions import Fraction

import pytest
from geom4d_scenarios import REPO_ROOT

from droneops_geom_4d import (
    DEFAULT_POLICY_RELPATH,
    PolicyInputError,
    ProtectedVolumePolicy,
    load_policy_file,
    policy_from_mapping,
    policy_from_yaml_text,
)
from droneops_geom_core import MICROS_PER_SECOND, AltitudeReference

pytestmark = pytest.mark.unit

VALID_VOLUME = {
    "horizontal_margin_m": 15,
    "vertical_margin_m": 10,
    "position_uncertainty_m": 5,
    "timing_uncertainty_s": 5,
    "transition_buffer_s": 10,
}


TOP_LEVEL = {
    "schema_version": 1,
    "policy_version": "unit-test-policy",
    "authoritative": False,
    "altitude_reference": "AGL",
}


def document(**overrides: object) -> dict[str, object]:
    """A policy document with top-level and margin overrides, kept separate."""
    top = dict(TOP_LEVEL)
    volume = dict(VALID_VOLUME)
    for key, value in overrides.items():
        if key in top:
            top[key] = value
        else:
            volume[key] = value
    return {**top, "protected_volume": volume}


def test_repository_policy_is_readable_and_illustrative() -> None:
    """The shipped file is the one everyone else will read."""
    policy = load_policy_file(REPO_ROOT / DEFAULT_POLICY_RELPATH)
    assert policy.schema_version == 1
    assert policy.policy_version == "sim-policy-v1"
    assert policy.altitude_reference is AltitudeReference.AGL
    assert policy.authoritative is False


def test_the_five_inputs_stay_five_separate_numbers() -> None:
    """Nothing is collapsed into one fudge factor."""
    policy = load_policy_file(REPO_ROOT / DEFAULT_POLICY_RELPATH)
    assert policy.horizontal_margin_mm == 15_000
    assert policy.vertical_margin_mm == 10_000
    assert policy.position_uncertainty_mm == 5_000
    assert policy.timing_uncertainty_us == 5 * MICROS_PER_SECOND
    assert policy.transition_buffer_us == 10 * MICROS_PER_SECOND
    assert policy.horizontal_reach_mm == 20_000
    assert policy.vertical_reach_mm == 10_000
    assert policy.timing_pad_us == policy.timing_uncertainty_us
    assert policy.horizontal_reach_mm != policy.position_uncertainty_mm


def test_metres_and_seconds_are_quantised_exactly_once() -> None:
    policy = policy_from_mapping(document(horizontal_margin_m=0.123456))
    assert policy.horizontal_margin_mm == 123
    assert policy.horizontal_reach_mm == 123 + 5_000


def test_transition_buffer_is_not_applied_as_a_margin() -> None:
    """Time slack stays in time: it never leaks into a horizontal reach."""
    policy = policy_from_mapping(document(transition_buffer_s=999))
    assert policy.horizontal_reach_mm == 20_000
    assert policy.vertical_reach_mm == 10_000
    assert policy.transition_pad_us == 999 * MICROS_PER_SECOND


def test_along_track_extension_rounds_outward() -> None:
    """A manoeuvre's lateral slack is never a hair smaller than the distance flown."""
    policy = policy_from_mapping(document())
    extension = policy.along_track_mm(Fraction(1, 3), 7)
    assert extension == math.ceil(7 / 3)
    zero = policy.along_track_mm(Fraction(0), 7)
    assert zero == 0


def test_policy_key_names_the_policy_a_verdict_used() -> None:
    first = policy_from_mapping(document())
    second = policy_from_mapping(document(horizontal_margin_m=16))
    assert first.key() != second.key()
    assert "policy=unit-test-policy" in first.key()
    assert first.key() == first.key()


def test_missing_margin_is_a_rejection_not_a_default() -> None:
    broken = document()
    del broken["protected_volume"]["position_uncertainty_m"]  # type: ignore[index]
    with pytest.raises(PolicyInputError, match="position_uncertainty_m"):
        policy_from_mapping(broken)


def test_unsupported_schema_version_is_a_rejection() -> None:
    with pytest.raises(PolicyInputError, match="schema_version"):
        policy_from_mapping(document(schema_version=7))


def test_unspecified_altitude_reference_cannot_evaluate_anything() -> None:
    """Invariant I9: a policy without a frame is not a policy."""
    with pytest.raises(PolicyInputError, match="altitude_reference"):
        policy_from_mapping(document(altitude_reference="UNSPECIFIED"))
    with pytest.raises(PolicyInputError, match="UNSPECIFIED"):
        ProtectedVolumePolicy(
            schema_version=1,
            policy_version="x",
            authoritative=False,
            altitude_reference=AltitudeReference.UNSPECIFIED,
            horizontal_margin_mm=1,
            vertical_margin_mm=1,
            position_uncertainty_mm=1,
            timing_uncertainty_us=1,
            transition_buffer_us=1,
        )


def test_negative_margin_is_rejected() -> None:
    with pytest.raises(PolicyInputError, match="non-negative"):
        policy_from_mapping(document(vertical_margin_m=-1))


def test_non_numeric_margin_is_rejected() -> None:
    with pytest.raises(PolicyInputError, match="must be a number"):
        policy_from_mapping(document(timing_uncertainty_s="soon"))


def test_yaml_text_and_missing_section_are_rejected() -> None:
    with pytest.raises(PolicyInputError, match="mapping"):
        policy_from_yaml_text("- just\n- a\n- list\n")
    with pytest.raises(PolicyInputError, match="protected_volume"):
        policy_from_yaml_text("schema_version: 1\n")
    with pytest.raises(PolicyInputError, match="no protected-volume policy"):
        load_policy_file(REPO_ROOT / "contracts" / "configuration" / "nope.yaml")
