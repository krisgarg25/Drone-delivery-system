"""Versioned protected-volume policy inputs, read from configuration, never inline.

Every margin this kernel applies to a swept volume comes from
``contracts/configuration/protected_volume_policy.yaml`` (see
``DEFAULT_POLICY_RELPATH``). The four inputs are kept **separate** on purpose, because
they answer different questions and a real operation tunes them independently:

``horizontal_margin_m``
    the separation the operation insists on between two bodies, laterally.
``vertical_margin_m``
    the same, in height. Kept separate because vertical uncertainty behaves
    differently from lateral uncertainty.
``position_uncertainty_m``
    how far off the planned track the body is allowed to be - navigation error,
    not intent.
``timing_uncertainty_s``
    how far the whole volume is widened in *time*: a late or early aircraft
    still occupies the space its plan says it will. Applied to **every** leg,
    level or climbing, because it is a statement about when, not about what the
    aircraft is doing.

``transition_buffer_s`` is the fifth input: slack for a **manoeuvre** running
late, applied by :meth:`ProtectedVolumePolicy.leg_pad_us` to a leg whose altitude
changes (climb, descent, layer transition) and to no other leg, and spent both on
the manoeuvre's time window and on the distance the body can still travel during
it (see :mod:`droneops_geom_4d.envelope`). The file states which reading applies;
both this kernel and the independent conflict checker refuse a file that states
another one, so agreement between the two implementations is about *method* and
never about the meaning of a margin.

Nothing here is a certified aviation number. Each value in the YAML file is an
**experiment parameter**, and the file itself is the place that is cited - the
safety requirement lives in the invariants and the test suite, not in a margin.
A policy that has not been validated carries ``authoritative: false``, and that
flag propagates into every verdict this package produces.

This module performs no arithmetic on wall-clock time and reads no environment
variable; the file path is always supplied explicitly by the caller so that a
run's ``config_release_sha`` can name the policy it used.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Final

import yaml

from droneops_geom_core import (
    MICROS_PER_SECOND,
    AltitudeReference,
    GeomCoreError,
    Microsecond,
    Millimetre,
    as_microseconds,
    metres_to_millimetres,
    reference_from_token,
)

from .errors import PolicyInputError

#: The policy schema this kernel speaks. A newer file is a rejection, not a guess.
SUPPORTED_SCHEMA_VERSION: Final[int] = 1

#: Where the authoritative copy lives, relative to a repository or bundle root.
#: Resolution is the caller's job: the kernel does not go looking for files.
DEFAULT_POLICY_RELPATH: Final[str] = "contracts/configuration/protected_volume_policy.yaml"

_MARGIN_KEYS: Final[tuple[str, ...]] = (
    "horizontal_margin_m",
    "vertical_margin_m",
    "position_uncertainty_m",
)
_TIME_KEYS: Final[tuple[str, ...]] = ("timing_uncertainty_s", "transition_buffer_s")

# --------------------------------------------------------------------------------
# Which leg each temporal margin applies to - read from the policy file, not inferred.
#
# ``timing_uncertainty_s`` answers "when is the aircraft where it says it is", so it
# pads **every** leg, level or climbing alike. ``transition_buffer_s`` answers "what
# if the manoeuvre runs late", so it pads only a leg that contains a manoeuvre - one
# whose altitude changes (climb, descent, layer transition), the same test
# :func:`droneops_geom_4d.envelope.Manoeuvre.for_leg` refuses to answer for a level
# leg. That division is ``technical-build/11`` section 5 read together with
# ``05-implementation-phases.md`` Phase 2 ("uncertainty *and transition buffers*") and
# it is the reading the independent conflict checker reaches from the same file by its
# own parser; the two must agree here because independence is about *method* (I1), not
# about policy semantics.
# --------------------------------------------------------------------------------
SUPPORTED_MEANING_VERSION: Final[int] = 2
TIMING_APPLIES_TO_EVERY_LEG: Final[str] = "every_leg"
TRANSITION_APPLIES_TO_MANOEUVRE_LEG: Final[str] = "manoeuvre_leg_only"
MANOEUVRE_TEST_LEG_ALTITUDE_CHANGES: Final[str] = "leg_altitude_changes"


def _check_semantics(
    meaning_version: int, timing: str, transition: str, manoeuvre_test: str
) -> None:
    """Refuse a policy that means something other than what this kernel implements.

    A silently reinterpreted separation rule is the most dangerous kind of config
    change, so an unexpected token is an error rather than a nudge: a kernel that
    quietly applied a manoeuvre buffer to level flight would convict plans no planner
    can produce, and a kernel that quietly dropped it would clear plans nobody meant to
    clear. Both answers would be wrong in a way no test could attribute.
    """
    if meaning_version != SUPPORTED_MEANING_VERSION:
        raise PolicyInputError(
            f"temporal semantics meaning_version {meaning_version} is not supported; this "
            f"kernel speaks meaning version {SUPPORTED_MEANING_VERSION}"
        )
    for name, value, wanted in (
        ("timing_uncertainty_applies_to", timing, TIMING_APPLIES_TO_EVERY_LEG),
        ("transition_buffer_applies_to", transition, TRANSITION_APPLIES_TO_MANOEUVRE_LEG),
        ("manoeuvre_test", manoeuvre_test, MANOEUVRE_TEST_LEG_ALTITUDE_CHANGES),
    ):
        if value != wanted:
            raise PolicyInputError(
                f"temporal semantics {name} = {value!r} is not implemented by this kernel, "
                f"which applies it to {wanted!r}; re-version the policy and both readers"
            )


@dataclass(frozen=True)
class ProtectedVolumePolicy:
    """The five versioned margins, in kernel units, plus their provenance.

    Units are integer: millimetres and microseconds. The YAML carries metres and
    seconds, and the quantisation happens exactly once, here, on the way in.
    """

    schema_version: int
    policy_version: str
    authoritative: bool
    altitude_reference: AltitudeReference
    horizontal_margin_mm: Millimetre
    vertical_margin_mm: Millimetre
    position_uncertainty_mm: Millimetre
    timing_uncertainty_us: Microsecond
    transition_buffer_us: Microsecond
    meaning_version: int = SUPPORTED_MEANING_VERSION
    timing_applies_to: str = TIMING_APPLIES_TO_EVERY_LEG
    transition_applies_to: str = TRANSITION_APPLIES_TO_MANOEUVRE_LEG
    manoeuvre_test: str = MANOEUVRE_TEST_LEG_ALTITUDE_CHANGES

    def __post_init__(self) -> None:
        if self.schema_version != SUPPORTED_SCHEMA_VERSION:
            raise PolicyInputError(
                f"protected_volume_policy schema_version {self.schema_version} is not "
                f"supported; this kernel speaks version {SUPPORTED_SCHEMA_VERSION}"
            )
        _check_semantics(
            self.meaning_version,
            self.timing_applies_to,
            self.transition_applies_to,
            self.manoeuvre_test,
        )
        if not self.policy_version.strip():
            raise PolicyInputError("policy_version must be a non-empty tag")
        if self.altitude_reference is AltitudeReference.UNSPECIFIED:
            raise PolicyInputError(
                "a protected-volume policy must state an altitude_reference; "
                "UNSPECIFIED cannot be evaluated against (invariant I9)"
            )
        for name, value in (
            ("horizontal_margin_mm", self.horizontal_margin_mm),
            ("vertical_margin_mm", self.vertical_margin_mm),
            ("position_uncertainty_mm", self.position_uncertainty_mm),
            ("timing_uncertainty_us", self.timing_uncertainty_us),
            ("transition_buffer_us", self.transition_buffer_us),
        ):
            if value < 0:
                raise PolicyInputError(f"{name} must be non-negative, got {value}")

    # --- derived quantities, spelled out so no arithmetic hides in a builder -----

    @property
    def horizontal_reach_mm(self) -> Millimetre:
        """Lateral reach of the *policy* alone: margin plus position uncertainty."""
        return Millimetre(self.horizontal_margin_mm + self.position_uncertainty_mm)

    @property
    def vertical_reach_mm(self) -> Millimetre:
        """Vertical reach of the *policy* alone: the vertical margin."""
        return Millimetre(self.vertical_margin_mm)

    @property
    def timing_pad_us(self) -> Microsecond:
        """How far each volume's time window is widened at both ends."""
        return Microsecond(self.timing_uncertainty_us)

    @property
    def transition_pad_us(self) -> Microsecond:
        """Manoeuvre slack: applied to climb/descent envelopes, not level flight."""
        return Microsecond(self.transition_buffer_us)

    def leg_pad_us(self, manoeuvring: bool) -> Microsecond:
        """The time pad one leg gets at each end - the policy rule, in one named place.

        ``manoeuvring`` is the policy's own test, ``leg_altitude_changes``: pass the
        leg's ``ProfileSegment.is_vertical_change``. A level leg is padded with timing
        uncertainty alone, because nothing about it is a manoeuvre; an
        altitude-changing leg is padded with timing uncertainty *and* the transition
        buffer. One rule, stated once, so no builder can redecide it locally and so the
        independent checker can be tested against the same sentence rather than against
        a comment.
        """
        pad = self.timing_uncertainty_us
        if manoeuvring:
            pad += self.transition_buffer_us
        return Microsecond(pad)

    def is_manoeuvre_leg(self, start_altitude_mm: int, end_altitude_mm: int) -> bool:
        """The declared manoeuvre test, applied to one leg's altitudes."""
        if self.manoeuvre_test != MANOEUVRE_TEST_LEG_ALTITUDE_CHANGES:  # pragma: no cover
            raise PolicyInputError(f"unimplemented manoeuvre test {self.manoeuvre_test!r}")
        return end_altitude_mm != start_altitude_mm

    def along_track_mm(self, speed_mm_per_us: Fraction, pad_us: int) -> Millimetre:
        """Distance covered at ``speed_mm_per_us`` during ``pad_us``, rounded outward.

        Rounding is always up: an envelope that shrank by a fraction of a
        millimetre would be a claim of clearance that was never made.
        """
        if pad_us < 0:
            raise PolicyInputError(f"pad_us must be non-negative, got {pad_us}")
        if speed_mm_per_us < 0:
            raise PolicyInputError(f"speed must be non-negative, got {speed_mm_per_us}")
        return Millimetre(-((-speed_mm_per_us * pad_us) // 1))

    def key(self) -> str:
        """Canonical identity of this policy, in a fixed field order.

        Deterministic by construction: the order is literal, not a ``dict`` or
        ``set`` iteration, so two runs that used the same policy agree on the
        string byte for byte and can be compared in a replay.
        """
        return "|".join(
            (
                f"schema={self.schema_version}",
                f"policy={self.policy_version}",
                (
                    f"meaning={self.meaning_version}:{self.timing_applies_to}"
                    f":{self.transition_applies_to}"
                ),
                f"ref={self.altitude_reference.value}",
                f"h={self.horizontal_margin_mm}",
                f"v={self.vertical_margin_mm}",
                f"pos={self.position_uncertainty_mm}",
                f"time={self.timing_uncertainty_us}",
                f"trans={self.transition_buffer_us}",
                f"auth={int(self.authoritative)}",
            )
        )


def _section(source: Mapping[str, object]) -> Mapping[str, object]:
    volume = source.get("protected_volume")
    if not isinstance(volume, dict):
        raise PolicyInputError(
            "protected_volume_policy.yaml must contain a `protected_volume` mapping"
        )
    return {str(key): value for key, value in volume.items()}


def _require(source: Mapping[str, object], key: str, section: str) -> object:
    if key not in source:
        raise PolicyInputError(f"protected_volume_policy.{key} is missing: {section}")
    return source[key]


def _metres_to_mm(value: object, key: str) -> Millimetre:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PolicyInputError(f"protected_volume_policy.{key} must be a number, got {value!r}")
    return metres_to_millimetres(float(value))


def _seconds_to_us(value: object, key: str) -> Microsecond:
    """Whole or fractional seconds to integer microseconds, floored once.

    The policy file carries integer seconds; a fractional value is still
    interpreted exactly (binary float to ``Fraction`` is lossless) and floored, so
    a policy can never gain padding by rounding.
    """
    if isinstance(value, bool):
        raise PolicyInputError(f"protected_volume_policy.{key} must be a number, got {value!r}")
    if isinstance(value, int):
        return as_microseconds(value * MICROS_PER_SECOND)
    if isinstance(value, float):
        return as_microseconds(math.floor(Fraction(value) * MICROS_PER_SECOND))
    raise PolicyInputError(f"protected_volume_policy.{key} must be a number, got {value!r}")


def _flag(value: object, key: str) -> bool:
    if isinstance(value, bool):
        return value
    raise PolicyInputError(f"protected_volume_policy.{key} must be true or false, got {value!r}")


def _semantics(source: Mapping[str, object]) -> dict[str, object]:
    """The declared meaning of the two temporal margins, or the canonical reading.

    Absent means "a file written before the meaning was pinned down", and such a file
    is read as the canonical version-2 meaning - the one this kernel implements - never
    as a licence to guess. A file that states a *different* meaning is rejected, so the
    default and the declaration can never diverge in silence.
    """
    block = source.get("temporal_semantics")
    if block is None:
        return {}
    if not isinstance(block, dict):
        raise PolicyInputError("protected_volume_policy.temporal_semantics must be a mapping")
    return {str(key): value for key, value in block.items()}


def _meaning_version(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PolicyInputError("temporal_semantics.meaning_version must be an integer")
    return int(value)


def policy_from_mapping(source: Mapping[str, object]) -> ProtectedVolumePolicy:
    """Build a policy from an already-parsed mapping. Pure: no file, no clock.

    Raises:
        PolicyInputError: a required key is missing, a margin is not a number,
            the reference is ``UNSPECIFIED`` or the schema version is unsupported.
    """
    flat = {str(key): value for key, value in source.items()}
    volume = _section(flat)
    schema_version = flat.get("schema_version")
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        raise PolicyInputError("protected_volume_policy.yaml needs an integer schema_version")
    policy_version = str(_require(flat, "policy_version", "top level"))
    reference_token = str(_require(flat, "altitude_reference", "top level"))
    try:
        reference = reference_from_token(reference_token)
    except GeomCoreError as exc:
        raise PolicyInputError(f"unknown altitude_reference {reference_token!r}") from exc
    authoritative = _flag(flat.get("authoritative", False), "authoritative")

    margins = {
        key: _metres_to_mm(_require(volume, key, "protected_volume"), key) for key in _MARGIN_KEYS
    }
    pads = {
        key: _seconds_to_us(_require(volume, key, "protected_volume"), key) for key in _TIME_KEYS
    }
    semantics = _semantics(flat)

    return ProtectedVolumePolicy(
        schema_version=schema_version,
        policy_version=policy_version,
        authoritative=authoritative,
        altitude_reference=reference,
        horizontal_margin_mm=margins["horizontal_margin_m"],
        vertical_margin_mm=margins["vertical_margin_m"],
        position_uncertainty_mm=margins["position_uncertainty_m"],
        timing_uncertainty_us=pads["timing_uncertainty_s"],
        transition_buffer_us=pads["transition_buffer_s"],
        meaning_version=_meaning_version(
            semantics.get("meaning_version", SUPPORTED_MEANING_VERSION)
        ),
        timing_applies_to=str(
            semantics.get("timing_uncertainty_applies_to", TIMING_APPLIES_TO_EVERY_LEG)
        ),
        transition_applies_to=str(
            semantics.get("transition_buffer_applies_to", TRANSITION_APPLIES_TO_MANOEUVRE_LEG)
        ),
        manoeuvre_test=str(semantics.get("manoeuvre_test", MANOEUVRE_TEST_LEG_ALTITUDE_CHANGES)),
    )


def policy_from_yaml_text(text: str) -> ProtectedVolumePolicy:
    """Parse the policy file's *contents*. Raises:

    PolicyInputError: the document is not a mapping.
    """
    loaded = yaml.safe_load(text)
    if not isinstance(loaded, dict):
        raise PolicyInputError("protected volume policy must be a YAML mapping")
    return policy_from_mapping({str(key): value for key, value in loaded.items()})


def load_policy_file(path: Path | str) -> ProtectedVolumePolicy:
    """Explicit boundary read of a policy file.

    The kernel itself never calls this: a run passes the path it froze, so the
    policy applied is part of the run's identity rather than something the
    geometry layer went and found.
    """
    candidate = Path(path)
    if not candidate.is_file():
        raise PolicyInputError(f"no protected-volume policy at {candidate}")
    return policy_from_yaml_text(candidate.read_text(encoding="utf-8"))
