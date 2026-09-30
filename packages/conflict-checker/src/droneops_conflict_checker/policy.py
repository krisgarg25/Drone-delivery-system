"""The checker's own reading of the versioned protected-volume policy.

This is a second, independent parser of
``contracts/configuration/protected_volume_policy.yaml`` and that duplication is
the deliverable, not an oversight: ``technical-build/11`` section 7 asks the
checker to "recompute protected volumes from raw plan points" and to use
"different geometry tools", and an ``.importlinter`` contract forbids importing
``droneops_geom_4d`` to get the margins second-hand. If the checker inherited the
planner's policy object it would inherit the planner's arithmetic on the planner's
terms, and agreement between the two would stop being evidence.

The differences from the planner's policy handling are deliberate and are the
conservatism this package exists to provide:

* :meth:`CheckerPolicy.leg_pad_us` applies **timing uncertainty and the transition
  buffer to every leg**. The planner pads timing uncertainty on every leg and adds
  the transition buffer only to a leg that changes height. A manoeuvre that runs
  late is a climb *or* a high-speed level pass, and the checker does not get to
  decide which ones are worth buffering.
* :meth:`CheckerPolicy.lateral_reach_mm` inflates the protected radius by the
  along-track distance the declared speed can cover inside that pad, rounded
  **up**. A volume that could only shrink if an aircraft was late is a claim of
  clearance nobody made.
* :data:`CONTACT_TOLERANCE_MM` is added to every contact test. The checker's
  geometry runs on shapely floats, so the tolerance is what makes a rounding error
  able to produce a false ``CONFLICT`` but not a false ``SAFE``.

Allow rules live here too, because they are policy: named, versioned, bounded by
a geometric floor they cannot waive, and loadable only from a caller-supplied
document so a run can name the release its exceptions came from.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from typing import Final

import yaml

from droneops_geom_core import (
    MICROS_PER_SECOND,
    AltitudeReference,
    GeomCoreError,
    as_microseconds,
    metres_to_millimetres,
    reference_from_token,
)

#: The policy schema this checker speaks. A newer file is a rejection, not a guess.
SUPPORTED_SCHEMA_VERSION: Final[int] = 1

#: Where the authoritative copy of the margins lives, relative to a repo or bundle root.
DEFAULT_POLICY_RELPATH: Final[str] = "contracts/configuration/protected_volume_policy.yaml"

#: Rounding added to every contact test so float error can only ever cause a
#: false conflict, never a false clearance.
CONTACT_TOLERANCE_MM: Final[int] = 1

_MARGIN_KEYS: Final[tuple[str, ...]] = (
    "horizontal_margin_m",
    "vertical_margin_m",
    "position_uncertainty_m",
)
_TIME_KEYS: Final[tuple[str, ...]] = ("timing_uncertainty_s", "transition_buffer_s")


class PolicyInputError(ValueError):
    """The policy document cannot be read as the contract requires.

    A subclass of ``ValueError`` rather than a bare ``GeomCoreError`` because the
    reader of this error is an operator deciding whether a release may proceed,
    and "the configuration is wrong" is the whole of the message.
    """


@dataclass(frozen=True)
class CheckerPolicy:
    """The five versioned margins, in kernel units, plus the checker's own additions.

    Units are integer: millimetres and microseconds. The YAML carries metres and
    seconds and the quantisation happens exactly once, here, on the way in.
    """

    schema_version: int
    policy_version: str
    authoritative: bool
    altitude_reference: AltitudeReference
    horizontal_margin_mm: int
    vertical_margin_mm: int
    position_uncertainty_mm: int
    timing_uncertainty_us: int
    transition_buffer_us: int

    def __post_init__(self) -> None:
        if self.schema_version != SUPPORTED_SCHEMA_VERSION:
            raise PolicyInputError(
                f"protected_volume_policy schema_version {self.schema_version} is not "
                f"supported; this checker speaks version {SUPPORTED_SCHEMA_VERSION}"
            )
        if not self.policy_version.strip():
            raise PolicyInputError("policy_version must be a non-empty tag")
        if self.altitude_reference is AltitudeReference.UNSPECIFIED:
            raise PolicyInputError(
                "a protected-volume policy must state an altitude_reference; UNSPECIFIED "
                "cannot be evaluated against (invariant I9)"
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

    # ------------------------------------------------------- derived quantities, in the open
    def leg_pad_us(self) -> int:
        """Absolute-time padding applied to both ends of every leg, without exception.

        Timing uncertainty plus transition buffer. The planner applies the
        transition buffer only to a leg that changes height; the checker does not
        discriminate, because deciding which manoeuvres are allowed to be late is
        exactly the decision a validator must not take on the planner's word.
        """
        return as_microseconds(self.timing_uncertainty_us + self.transition_buffer_us)

    def crossing_tolerance_us(self) -> int:
        """How close two arrivals at a shared point count as simultaneous.

        Two independent margins add here: each body may be late by its own timing
        uncertainty, and the pair may both be in a transition.
        """
        return as_microseconds(
            2 * (self.timing_uncertainty_us + self.transition_buffer_us)
        )

    def along_track_slack_mm(self, speed_mm_per_us: Fraction, pad_us: int) -> int:
        """Distance the declared speed covers in ``pad_us``, rounded up to a whole mm."""
        if speed_mm_per_us < 0:
            raise PolicyInputError(f"speed cannot be negative, got {speed_mm_per_us}")
        travel = -((-speed_mm_per_us * max(0, pad_us)) // 1)
        return int(math.ceil(travel))

    def lateral_reach_mm(self, body_radius_mm: int, along_track_mm: int = 0) -> int:
        """How far laterally the protected volume of a body reaches.

        ``body radius + horizontal margin + position uncertainty``, then the
        along-track slack, then :data:`CONTACT_TOLERANCE_MM`. The distinction
        between "how far the aircraft is from the track" (uncertainty) and "how
        late it may be" (timing) is preserved by keeping the margins separate in
        the file and adding them here rather than collapsing them into one number.
        """
        if body_radius_mm < 0:
            raise PolicyInputError(f"body radius cannot be negative, got {body_radius_mm}")
        if along_track_mm < 0:
            raise PolicyInputError(f"along-track slack cannot be negative, got {along_track_mm}")
        return int(
            body_radius_mm
            + self.horizontal_margin_mm
            + self.position_uncertainty_mm
            + along_track_mm
            + CONTACT_TOLERANCE_MM
        )

    def vertical_reach_mm(self, body_half_height_mm: int) -> int:
        """How far the protected volume reaches above and below the reported height."""
        if body_half_height_mm < 0:
            raise PolicyInputError(
                f"body half height cannot be negative, got {body_half_height_mm}"
            )
        return int(body_half_height_mm + self.vertical_margin_mm + CONTACT_TOLERANCE_MM)

    def key(self) -> str:
        """Canonical identity of this policy release, in a literal field order.

        Not ``dict`` or ``set`` iteration: two runs that used the same file must
        agree on this string byte for byte for replay to be comparable (I7).
        """
        return "|".join(
            (
                f"schema={self.schema_version}",
                f"policy={self.policy_version}",
                f"ref={self.altitude_reference.value}",
                f"h={self.horizontal_margin_mm}",
                f"v={self.vertical_margin_mm}",
                f"pos={self.position_uncertainty_mm}",
                f"time={self.timing_uncertainty_us}",
                f"trans={self.transition_buffer_us}",
                f"auth={int(self.authoritative)}",
                "path=checker-own-parser",
            )
        )


# --------------------------------------------------------------------- document reading
def _mapping(source: Mapping[str, object], section: str) -> Mapping[str, object]:
    if section == "":
        return {str(key): value for key, value in source.items()}
    nested = dict(source).get(section)
    if not isinstance(nested, dict):
        raise PolicyInputError(f"protected_volume_policy.yaml must contain a `{section}` mapping")
    return {str(key): value for key, value in nested.items()}


def _required(source: Mapping[str, object], key: str) -> object:
    if key not in source:
        raise PolicyInputError(f"protected_volume_policy.{key} is missing")
    return source[key]


def _millimetres(value: object, key: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PolicyInputError(f"protected_volume_policy.{key} must be a number, got {value!r}")
    return int(metres_to_millimetres(float(value)))


def _microseconds(value: object, key: str) -> int:
    """Whole or fractional seconds to integer microseconds, floored once.

    Flooring is deliberate: padding rounded up would let a policy claim slack it
    did not ask for, and slack the policy did not ask for is a margin nobody
    signed off on.
    """
    if isinstance(value, bool):
        raise PolicyInputError(f"protected_volume_policy.{key} must be a number, got {value!r}")
    if isinstance(value, int):
        return as_microseconds(value * MICROS_PER_SECOND)
    if isinstance(value, float):
        return as_microseconds(math.floor(Fraction(value) * MICROS_PER_SECOND))
    raise PolicyInputError(f"protected_volume_policy.{key} must be a number, got {value!r}")


def _boolean(value: object, key: str) -> bool:
    if isinstance(value, bool):
        return value
    raise PolicyInputError(f"protected_volume_policy.{key} must be true or false, got {value!r}")


def policy_from_mapping(source: Mapping[str, object]) -> CheckerPolicy:
    """Build a policy from an already-parsed mapping. Pure: no file, no clock.

    Raises:
        PolicyInputError: a required key is missing, a margin is not a number, the
            reference is ``UNSPECIFIED``, or the schema version is unsupported.
    """
    top = _mapping(source, "")
    volume = _mapping(source, "protected_volume")
    schema = top.get("schema_version")
    if isinstance(schema, bool) or not isinstance(schema, int):
        raise PolicyInputError("protected_volume_policy.yaml needs an integer schema_version")
    reference_token = str(_required(top, "altitude_reference"))
    try:
        reference = reference_from_token(reference_token)
    except GeomCoreError as error:
        raise PolicyInputError(f"unknown altitude_reference {reference_token!r}") from error
    margins = {key: _millimetres(_required(volume, key), key) for key in _MARGIN_KEYS}
    pads = {key: _microseconds(_required(volume, key), key) for key in _TIME_KEYS}
    return CheckerPolicy(
        schema_version=schema,
        policy_version=str(_required(top, "policy_version")),
        authoritative=_boolean(top.get("authoritative", False), "authoritative"),
        altitude_reference=reference,
        horizontal_margin_mm=margins["horizontal_margin_m"],
        vertical_margin_mm=margins["vertical_margin_m"],
        position_uncertainty_mm=margins["position_uncertainty_m"],
        timing_uncertainty_us=pads["timing_uncertainty_s"],
        transition_buffer_us=pads["transition_buffer_s"],
    )


def policy_from_yaml_text(text: str) -> CheckerPolicy:
    """Parse the contents of a policy document.

    Raises:
        PolicyInputError: the document is not a mapping.
    """
    loaded = yaml.safe_load(text)
    if not isinstance(loaded, dict):
        raise PolicyInputError("protected volume policy must be a YAML mapping")
    return policy_from_mapping({str(key): value for key, value in loaded.items()})


def load_policy_file(path: Path | str) -> CheckerPolicy:
    """Explicit boundary read. Nothing in this package goes looking for files.

    The caller supplies the path it froze for the run, so the margins applied are
    part of the run's identity instead of something the validator went and found.
    """
    candidate = Path(path)
    if not candidate.is_file():
        raise PolicyInputError(f"no protected-volume policy at {candidate}")
    return policy_from_yaml_text(candidate.read_text(encoding="utf-8"))


# ------------------------------------------------------------------------- allow rules
@dataclass(frozen=True)
class AllowRule:
    """One named, versioned exception to the no-overlap rule.

    An exception is not a veto the planner holds: :class:`AllowRuleSet` only ever
    grants when the *checker's own measurement* fits inside ``max_contact_span_us``
    and ``min_separation_mm``, and the rule is only in force when both records of
    the pair name it by id *and* version. The floor is a floor - a rule can lower
    how long an interaction may last, it cannot raise how close two bodies may
    come, because that is the number the operation's safety case is about.
    """

    rule_id: str
    rule_version: str
    policy_version: str
    rationale: str
    approved_by: str
    max_contact_span_us: int
    min_separation_mm: int
    check_names: frozenset[str] = field(default_factory=frozenset)
    corridor_pairs: frozenset[tuple[str, str]] = field(default_factory=frozenset)
    plan_pairs: frozenset[tuple[str, str]] = field(default_factory=frozenset)
    active: bool = True
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.rule_id.strip() or not self.rule_version.strip():
            raise PolicyInputError("an allow rule needs both an id and a version")
        if not self.rationale.strip() or not self.approved_by.strip():
            raise PolicyInputError(
                f"allow rule {self.rule_id}@{self.rule_version} needs a rationale and an approver"
            )
        if self.max_contact_span_us < 0 or self.min_separation_mm < 0:
            raise PolicyInputError(f"allow rule {self.rule_id} has a negative bound")

    def covers_pair(self, left_plan: str, right_plan: str) -> bool:
        """Whether this rule is about this pair of plan versions at all."""
        if not self.plan_pairs:
            return True
        return (left_plan, right_plan) in self.plan_pairs or (right_plan, left_plan) in self.plan_pairs

    def covers_corridors(self, keys: Sequence[str]) -> bool:
        """Whether any claimed corridor pair of the interaction is one this rule names."""
        if not self.corridor_pairs:
            return True
        wanted = {tuple(sorted(pair)) for pair in self.corridor_pairs}
        for left in keys:
            for right in keys:
                if left != right and (left, right) in wanted:
                    return True
        return False

    def covers_checks(self, names: Sequence[str]) -> bool:
        """A rule that names no check covers none, and must name *every* one it excuses.

        An empty ``names`` is also false: a contact list that names nothing is a
        caller that has not decided what it is excepting, and an exception granted to
        nothing at all would be an exception granted to everything.
        """
        if not self.check_names or not names:
            return False
        return all(name in self.check_names for name in names)

    def in_force(self, now: datetime | None) -> bool:
        if not self.active:
            return False
        if self.expires_at is None or now is None:
            return True
        return now <= self.expires_at


@dataclass(frozen=True)
class AllowRuleSet:
    """The exceptions a run is willing to consider, keyed by id and version."""

    rules: tuple[AllowRule, ...] = ()

    def __post_init__(self) -> None:
        seen: set[tuple[str, str]] = set()
        for rule in self.rules:
            identity = (rule.rule_id, rule.rule_version)
            if identity in seen:
                raise PolicyInputError(f"allow rule {rule.rule_id}@{rule.rule_version} is duplicated")
            seen.add(identity)

    @classmethod
    def empty(cls) -> AllowRuleSet:
        """The default: no exception exists unless somebody wrote one down."""
        return cls(rules=())

    def find(self, rule_id: str | None, rule_version: str | None) -> AllowRule | None:
        """Exact id-and-version lookup. A near miss is not a match."""
        if rule_id is None or rule_version is None:
            return None
        for rule in self.rules:
            if rule.rule_id == rule_id and rule.rule_version == rule_version:
                return rule
        return None

    def named(self) -> tuple[str, ...]:
        """Sorted ``id@version`` of every rule present, for a trace footer."""
        return tuple(sorted(f"{rule.rule_id}@{rule.rule_version}" for rule in self.rules))


def allow_rule_from_mapping(source: Mapping[str, object]) -> AllowRule:
    """One allow rule from a parsed document. Raises: PolicyInputError."""
    item = {str(key): value for key, value in source.items()}
    checks = item.get("checks", [])
    if not isinstance(checks, list):
        raise PolicyInputError("allow rule `checks` must be a list of check names")
    corridors = item.get("corridor_pairs", [])
    if not isinstance(corridors, list):
        raise PolicyInputError("allow rule `corridor_pairs` must be a list of two-element lists")
    plans = item.get("plan_pairs", [])
    if not isinstance(plans, list):
        raise PolicyInputError("allow rule `plan_pairs` must be a list of two-element lists")
    span = item.get("max_contact_span_us")
    floor = item.get("min_separation_mm")
    if isinstance(span, bool) or not isinstance(span, int):
        raise PolicyInputError("allow rule needs an integer max_contact_span_us")
    if isinstance(floor, bool) or not isinstance(floor, int):
        raise PolicyInputError("allow rule needs an integer min_separation_mm")
    expiry = item.get("expires_at")
    expires_at = datetime.fromisoformat(str(expiry)) if expiry is not None else None
    return AllowRule(
        rule_id=str(item.get("rule_id", "")),
        rule_version=str(item.get("rule_version", "")),
        policy_version=str(item.get("policy_version", "")),
        rationale=str(item.get("rationale", "")),
        approved_by=str(item.get("approved_by", "")),
        max_contact_span_us=span,
        min_separation_mm=floor,
        check_names=frozenset(str(name) for name in checks),
        corridor_pairs=frozenset(_pair(entry, "corridor_pairs") for entry in corridors),
        plan_pairs=frozenset(_pair(entry, "plan_pairs") for entry in plans),
        active=bool(item.get("active", True)),
        expires_at=expires_at,
    )


def _pair(entry: object, field_name: str) -> tuple[str, str]:
    if not isinstance(entry, list) or len(entry) != 2:
        raise PolicyInputError(f"allow rule {field_name} entries must be two-element lists")
    return (str(entry[0]), str(entry[1]))


def allow_rules_from_yaml_text(text: str) -> AllowRuleSet:
    """Parse a document of ``allow_rules: [...]``. Raises: PolicyInputError."""
    loaded = yaml.safe_load(text)
    if not isinstance(loaded, dict):
        raise PolicyInputError("allow rules document must be a YAML mapping")
    entries = dict(loaded).get("allow_rules")
    if entries is None:
        return AllowRuleSet.empty()
    if not isinstance(entries, list):
        raise PolicyInputError("allow_rules must be a list")
    return AllowRuleSet(
        rules=tuple(
            sorted(
                (allow_rule_from_mapping({str(k): v for k, v in entry.items()}) for entry in entries
                 if isinstance(entry, dict)),
                key=lambda rule: (rule.rule_id, rule.rule_version),
            )
        )
    )
