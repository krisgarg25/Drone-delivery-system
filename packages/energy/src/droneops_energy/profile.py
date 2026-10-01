"""Versioned, content-hashed, explicitly illustrative energy profiles.

Nothing about the battery model is hard-coded in the consumption kernel: masses,
power curves, climb costs, wind coefficients, degradation curves and reserve
fractions all arrive as one named, versioned, hashed object
(:class:`BatteryProfileVersion`) and every output repeats the hash it was
computed under. A run whose profile hash differs from a previous run's is a
different experiment, and the difference is visible in the record.

The committed profiles in ``contracts/vehicle-profile/*.yaml`` are synthetic
placeholders, and the coefficients they do not contain (climb cost, wind
coupling, degradation, gust uplift) are invented illustrative parameters. Until
vendor or measured pilot data replaces them, every result from this package says
so - see :data:`ILLUSTRATIVE_DISCLAIMER`.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields
from typing import Final

from .errors import EnergyInputError, ProfileConflictError
from .numeric import (
    BP_PER_UNIT,
    CurvePoint,
    apply_bp,
    ceil_div,
    format_curve,
    interpolate_curve,
    parse_curve,
    require,
    validate_curve,
)

#: Present on every output of this package. It is a constant, not a default: a
#: result cannot be produced without carrying the basis statement with it.
ILLUSTRATIVE_DISCLAIMER: Final = (
    "Energy basis is ILLUSTRATIVE: parameters are a named, versioned simulation "
    "profile, not vendor or measured pilot data. Simulation only. Not authorised "
    "for flight."
)

#: The type-certification status recorded for every committed synthetic profile.
UNVERIFIED_SIMULATED: Final = "unverified-simulated"

#: Curve fields, resolved through :func:`parse_curve` when a profile is loaded.
CURVE_FIELDS: Final = frozenset(
    {"power_by_mass", "power_by_airspeed", "power_by_soh", "usable_by_soh"}
)

#: Integer (non-curve, non-flag) fields, range-checked on load.
RANGE_FIELDS: Final = frozenset(
    {
        "climb_mwh_per_kg_per_m",
        "descent_recovery_bp",
        "gust_threshold_dmps",
        "gust_uplift_bp_per_dmps",
        "gust_uplift_cap_bp",
        "reserve_fraction_bp",
        "alternate_fraction_bp",
    }
)


def _flag(mapping: Mapping[str, str], key: str) -> bool:
    token = mapping[key].strip().lower()
    if token not in {"true", "false"}:
        raise EnergyInputError(f"profile field {key} must be 'true' or 'false'")
    return token == "true"


@dataclass(frozen=True)
class BatteryProfileVersion:
    """One immutable, addressable set of energy parameters.

    Address it by ``(name, version)``; verify it by :attr:`parameters_sha256`.
    ``power_by_*`` and ``usable_by_soh`` curves are basis-point characteristics
    measured against the reference condition, which keeps every derived number
    integer.
    """

    # identity and basis -------------------------------------------------------
    name: str
    version: str
    source: str
    type_cert_status: str
    illustrative: bool
    authoritative: bool
    # masses ------------------------------------------------------------------
    empty_mass_g: int
    max_payload_g: int
    max_takeoff_mass_g: int
    reference_mass_g: int
    # speeds ------------------------------------------------------------------
    cruise_airspeed_dmps: int
    reference_airspeed_dmps: int
    max_wind_dmps: int
    # battery -----------------------------------------------------------------
    capacity_wh: int
    usable_by_soh: tuple[CurvePoint, ...]
    power_by_soh: tuple[CurvePoint, ...]
    # power -------------------------------------------------------------------
    cruise_power_w: int
    hover_power_w: int
    power_by_mass: tuple[CurvePoint, ...]
    power_by_airspeed: tuple[CurvePoint, ...]
    # vertical ----------------------------------------------------------------
    climb_mwh_per_kg_per_m: int
    descent_recovery_bp: int
    # wind --------------------------------------------------------------------
    gust_threshold_dmps: int
    gust_uplift_bp_per_dmps: int
    gust_uplift_cap_bp: int
    # reserve policy ----------------------------------------------------------
    reserve_fraction_bp: int
    alternate_fraction_bp: int

    def __post_init__(self) -> None:
        require(self.name != "", "profile name is required")
        require(self.version != "", "profile version is required")
        require(self.source != "", "profile source is required: where did these numbers come from")
        require(self.type_cert_status != "", "profile type_cert_status is required")
        require(self.capacity_wh > 0, f"{self.name}: capacity_wh must be positive")
        require(self.cruise_power_w > 0, f"{self.name}: cruise_power_w must be positive")
        require(self.hover_power_w > 0, f"{self.name}: hover_power_w must be positive")
        require(self.climb_mwh_per_kg_per_m >= 0, "climb cost cannot be negative")
        require(self.empty_mass_g > 0, f"{self.name}: empty_mass_g must be positive")
        require(
            self.max_takeoff_mass_g >= self.empty_mass_g,
            f"{self.name}: max takeoff mass below empty mass",
        )
        require(
            0 <= self.descent_recovery_bp < BP_PER_UNIT,
            "descent recovery must be in [0, 10000) bp: a descent cannot pay for itself",
        )
        for field_name in ("reserve_fraction_bp", "alternate_fraction_bp"):
            value = getattr(self, field_name)
            require(0 <= value <= BP_PER_UNIT, f"{field_name} must be a fraction in basis points")
        require(
            self.gust_uplift_cap_bp >= 0 and self.gust_uplift_bp_per_dmps >= 0,
            "gust uplift coefficients cannot be negative",
        )
        for curve_name in CURVE_FIELDS:
            curve = validate_curve(getattr(self, curve_name))
            require(
                len(curve) >= 2,
                f"{self.name}: {curve_name} needs at least two points to be a curve",
            )
        # Claiming a parameter is *not* illustrative is a claim of authority; it
        # cannot be made by leaving a flag out, so the combination is refused.
        if not self.illustrative and not self.authoritative:
            raise EnergyInputError(
                f"{self.name}@{self.version}: a profile may only drop the illustrative flag "
                "by being explicitly authoritative (vendor or field data), which this is not"
            )

    # -------------------------------------------------------------- addressing
    @property
    def profile_id(self) -> str:
        """The name dispatch and persistence should record: version, not a blob."""
        return f"{self.name}@{self.version}"

    @property
    def canonical_parameters(self) -> dict[str, str]:
        """Every parameter as a canonical string, sorted, ready to hash."""
        rows: dict[str, str] = {}
        for field in fields(self):
            value = getattr(self, field.name)
            if isinstance(value, bool):
                rows[field.name] = "true" if value else "false"
            elif isinstance(value, int):
                rows[field.name] = str(value)
            elif isinstance(value, str):
                rows[field.name] = value
            else:
                rows[field.name] = format_curve(tuple(value))
        return dict(sorted(rows.items()))

    @property
    def parameters_sha256(self) -> str:
        """Content hash of the whole parameter set: the pin a run records."""
        payload = "\n".join(f"{key}={value}" for key, value in self.canonical_parameters.items())
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @property
    def basis(self) -> str:
        """The basis statement, tuned to what this profile actually claims."""
        if self.illustrative:
            return ILLUSTRATIVE_DISCLAIMER
        return f"Energy basis: authoritative profile {self.profile_id} ({self.source})."

    @property
    def certification_status(self) -> str:
        """Type-certification status: always ``unverified-simulated`` for now."""
        return self.type_cert_status

    # ------------------------------------------------------------- loading
    @classmethod
    def from_mapping(cls, mapping: Mapping[str, str]) -> BatteryProfileVersion:
        """Build a profile from a plain string mapping (a config release row).

        Adapters and applications read files; the kernel receives the parsed
        result. Unknown keys are refused rather than ignored, so a typo in a
        battery constant cannot quietly become "the default".
        """
        known = {field.name for field in fields(cls)}
        extra = sorted(set(mapping) - known)
        if extra:
            raise EnergyInputError(f"unknown profile fields: {extra}")
        missing = sorted(known - set(mapping))
        if missing:
            raise EnergyInputError(f"profile is missing fields: {missing}")
        values: dict[str, object] = {}
        for key, text in mapping.items():
            if key in CURVE_FIELDS:
                values[key] = parse_curve(text)
            elif key in {"illustrative", "authoritative"}:
                values[key] = _flag({"key": text}, "key")
            elif key in {"name", "version", "source", "type_cert_status"}:
                values[key] = text
            else:
                try:
                    values[key] = int(text)
                except ValueError as error:
                    raise EnergyInputError(f"profile field {key} is not an integer") from error
        return cls(**values)  # type: ignore[arg-type]

    def to_mapping(self) -> dict[str, str]:
        """The canonical string form, for a config release or a stored record."""
        return dict(self.canonical_parameters)

    def verify_hash(self, expected_sha256: str) -> BatteryProfileVersion:
        """Refuse a profile that does not match the hash a caller pinned."""
        actual = self.parameters_sha256
        if actual != expected_sha256:
            raise ProfileConflictError(
                f"profile {self.profile_id}: hash is {actual}, pinned as {expected_sha256}. "
                "A published version must never be edited in place."
            )
        return self

    # ------------------------------------------------------------ mass checks
    @property
    def loaded_mass_g(self) -> int:
        """Empty mass at maximum payload: the worst-case takeoff mass of the type."""
        return self.empty_mass_g + self.max_payload_g

    def mass_for_payload(self, payload_g: int) -> int:
        """All-up mass for a payload, or a refusal if the type cannot carry it."""
        require(payload_g >= 0, f"{self.name}: payload cannot be negative")
        if payload_g > self.max_payload_g:
            raise EnergyInputError(
                f"{self.name}: payload {payload_g} g exceeds profile limit {self.max_payload_g} g"
            )
        total = self.empty_mass_g + payload_g
        if total > self.max_takeoff_mass_g:
            raise EnergyInputError(
                f"{self.name}: all-up mass {total} g exceeds max takeoff mass "
                f"{self.max_takeoff_mass_g} g"
            )
        return total

    # ------------------------------------------------------------- the curves
    def power_w(self, *, mass_g: int, airspeed_dmps: int, soh_bp: int) -> int:
        """Shaft power for a mass and commanded airspeed, rounded up.

        Power is the reference cruise power scaled by three tabulated curves
        (mass, airspeed, battery age). All scaling is integer basis-point math.
        """
        require(0 <= soh_bp <= BP_PER_UNIT, "state of health must be in basis points")
        mass_bp = interpolate_curve(
            self.power_by_mass, mass_g, round_up=True, name=f"{self.name}.power_by_mass"
        )
        speed_bp = interpolate_curve(
            self.power_by_airspeed,
            airspeed_dmps,
            round_up=True,
            name=f"{self.name}.power_by_airspeed",
        )
        age_bp = interpolate_curve(
            self.power_by_soh, soh_bp, round_up=True, name=f"{self.name}.power_by_soh"
        )
        numerator = self.cruise_power_w * mass_bp * speed_bp * age_bp
        return ceil_div(numerator, BP_PER_UNIT**3)

    def hover_power_at(self, *, mass_g: int, soh_bp: int) -> int:
        """Power to hold position, at loaded mass.

        The mass curve is tabulated at cruise; applying it to hover is part of the
        illustrative model structure, not a measured effect.
        """
        mass_bp = interpolate_curve(
            self.power_by_mass, mass_g, round_up=True, name=f"{self.name}.power_by_mass"
        )
        age_bp = interpolate_curve(
            self.power_by_soh, soh_bp, round_up=True, name=f"{self.name}.power_by_soh"
        )
        return ceil_div(self.hover_power_w * mass_bp * age_bp, BP_PER_UNIT**2)

    def climb_mwh(self, *, mass_g: int, climb_mm: int) -> int:
        """Energy to lift ``mass_g`` by ``climb_mm``, rounded up."""
        if climb_mm <= 0:
            return 0
        raw = mass_g * climb_mm * self.climb_mwh_per_kg_per_m
        return ceil_div(raw, 1_000_000)

    def descent_credit_mwh(self, *, mass_g: int, descent_mm: int) -> int:
        """Energy recovered from descent, rounded *down*: never bank a credit."""
        if descent_mm <= 0 or self.descent_recovery_bp <= 0:
            return 0
        raw = mass_g * descent_mm * self.climb_mwh_per_kg_per_m
        gross = raw // 1_000_000
        return apply_bp(gross, self.descent_recovery_bp, round_up=False)

    def gust_uplift_bp(self, *, sustained_dmps: int, gust_dmps: int) -> int:
        """Reserve uplift for gustiness, in basis points of mission consumption.

        Sustained wind enters the consumption model through the wind triangle;
        gusts do not, because a peak gust is not a steady force averaged over a leg.
        They raise the *required margin* instead, and only the excess of the peak
        over the sustained speed counts: a trace that reports no gust column
        produces exactly zero uplift rather than an invented one. The coefficient
        itself is an illustrative policy number, not a measured handling quality.
        """
        excess = (gust_dmps - sustained_dmps) - self.gust_threshold_dmps
        if excess <= 0:
            return 0
        return min(excess * self.gust_uplift_bp_per_dmps, self.gust_uplift_cap_bp)

    # -------------------------------------------------------------- battery
    def usable_capacity_wh(self, soh_bp: int) -> int:
        """Energy an aged pack can actually deliver from full, rounded down."""
        require(0 <= soh_bp <= BP_PER_UNIT, "state of health must be in basis points")
        retained = interpolate_curve(
            self.usable_by_soh, soh_bp, round_up=False, name=f"{self.name}.usable_by_soh"
        )
        return apply_bp(self.capacity_wh, retained, round_up=False)

    def available_wh(self, *, soh_bp: int, charge_bp: int) -> int:
        """Energy currently in the pack above the usable-capacity reference, down.

        The reserve requirement is charged against this, so the discharge floor is
        never applied twice.
        """
        require(0 <= charge_bp <= BP_PER_UNIT, "charge must be in basis points")
        return apply_bp(self.usable_capacity_wh(soh_bp), charge_bp, round_up=False)

    def reserve_wh(self, *, soh_bp: int, for_alternate: bool) -> int:
        """The reserve term of the complete-mission sum, in whole watt-hours.

        ``alternate_fraction_bp`` is added when recovery is to an alternate rather
        than home: an alternate has no guaranteed charger, crew or pad, so the
        operator policy holds extra energy there.
        """
        usable = self.usable_capacity_wh(soh_bp)
        fraction = self.reserve_fraction_bp
        if for_alternate:
            fraction += self.alternate_fraction_bp
        return apply_bp(usable, min(fraction, BP_PER_UNIT), round_up=False)

    def describe(self) -> Sequence[str]:
        """Human-readable parameter dump for a review record."""
        return tuple(f"{key}={value}" for key, value in self.canonical_parameters.items())
