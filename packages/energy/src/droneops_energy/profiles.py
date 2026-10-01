"""The committed illustrative profiles, expressed as data rather than code.

``contracts/vehicle-profile/airframe-x-sim.yaml`` and ``airframe-y-sim.yaml``
supply the mass, speed, capacity, power and reserve numbers below; every field
those files do not contain (climb cost, wind coupling, degradation curves, gust
uplift) is an invented placeholder recorded as such in
:data:`PARAMETER_PROVENANCE`. Both profiles are built through
:meth:`droneops_energy.profile.BatteryProfileVersion.from_mapping`, the same path
a config release uses, so the kernel demonstrably has no private constants.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from .errors import UnknownProfileError
from .profile import UNVERIFIED_SIMULATED, BatteryProfileVersion

#: Where each parameter came from. Reviewers need this to know what to replace.
PARAMETER_PROVENANCE: Final[Mapping[str, str]] = {
    "empty_mass_g": "contracts/vehicle-profile (illustrative)",
    "max_payload_g": "contracts/vehicle-profile (illustrative)",
    "cruise_airspeed_dmps": "contracts/vehicle-profile cruise_speed_ms (illustrative)",
    "max_wind_dmps": "contracts/vehicle-profile max_wind_ms (illustrative)",
    "capacity_wh": "contracts/vehicle-profile battery_profile (illustrative)",
    "cruise_power_w": "contracts/vehicle-profile battery_profile (illustrative)",
    "hover_power_w": "contracts/vehicle-profile battery_profile (illustrative)",
    "reserve_fraction_bp": "contracts/vehicle-profile reserve_fraction (illustrative)",
    "type_cert_status": "contracts/vehicle-profile type_cert_status",
    "max_takeoff_mass_g": "derived: empty + max payload (illustrative)",
    "reference_mass_g": "illustrative placeholder: no vendor data",
    "reference_airspeed_dmps": "illustrative placeholder: no vendor data",
    "power_by_mass": "illustrative placeholder: sqrt(mass) shape, no measured points",
    "power_by_airspeed": "illustrative placeholder: power bathtub, no measured points",
    "power_by_soh": "illustrative placeholder: rising internal resistance, unmeasured",
    "usable_by_soh": "illustrative placeholder: capacity fade knee, unmeasured",
    "climb_mwh_per_kg_per_m": "illustrative placeholder: g*h/efficiency, efficiency unmeasured",
    "descent_recovery_bp": "illustrative placeholder: no measured recovery",
    "gust_threshold_dmps": "illustrative policy: operational preference, not a limit",
    "gust_uplift_bp_per_dmps": "illustrative policy: reserve uplift, no flight-test basis",
    "gust_uplift_cap_bp": "illustrative policy: uplift ceiling",
    "alternate_fraction_bp": "illustrative policy: extra reserve when recovering off-home",
}

#: Fields that are not in the committed YAML at all: what a reviewer must replace.
ILLUSTRATIVE_ONLY_FIELDS: Final = frozenset(
    name for name, origin in PARAMETER_PROVENANCE.items() if origin.startswith("illustrative")
)

_BASE: Final[Mapping[str, str]] = {
    "type_cert_status": UNVERIFIED_SIMULATED,
    "illustrative": "true",
    "authoritative": "false",
    "descent_recovery_bp": "1200",
    "gust_threshold_dmps": "10",
    "gust_uplift_bp_per_dmps": "50",
    "gust_uplift_cap_bp": "2500",
    "power_by_soh": "6000:11800,7000:11100,8000:10600,9000:10250,10000:10000",
    "usable_by_soh": "6000:5750,7000:7000,8000:8000,9000:9000,10000:10000",
}

_AIRFRAME_X_SIM: Final[Mapping[str, str]] = {
    **_BASE,
    "name": "airframe-x-sim",
    "version": "illustrative-1",
    "source": "contracts/vehicle-profile/airframe-x-sim.yaml",
    "empty_mass_g": "2400",
    "max_payload_g": "2000",
    "max_takeoff_mass_g": "4400",
    "reference_mass_g": "3400",
    "cruise_airspeed_dmps": "120",
    "reference_airspeed_dmps": "120",
    "max_wind_dmps": "80",
    "capacity_wh": "400",
    "cruise_power_w": "180",
    "hover_power_w": "260",
    "power_by_mass": "2400:8400,3400:10000,4400:11400",
    "power_by_airspeed": "40:13500,80:10800,120:10000,160:10900,200:12600",
    "climb_mwh_per_kg_per_m": "5",
    "reserve_fraction_bp": "2500",
    "alternate_fraction_bp": "800",
}

_AIRFRAME_Y_SIM: Final[Mapping[str, str]] = {
    **_BASE,
    "name": "airframe-y-sim",
    "version": "illustrative-1",
    "source": "contracts/vehicle-profile/airframe-y-sim.yaml",
    "empty_mass_g": "3600",
    "max_payload_g": "3500",
    "max_takeoff_mass_g": "7100",
    "reference_mass_g": "5000",
    "cruise_airspeed_dmps": "100",
    "reference_airspeed_dmps": "100",
    "max_wind_dmps": "100",
    "capacity_wh": "650",
    "cruise_power_w": "240",
    "hover_power_w": "340",
    "power_by_mass": "3600:8500,5000:10000,7100:11900",
    "power_by_airspeed": "40:13500,70:10900,100:10000,140:10900,180:12600",
    "climb_mwh_per_kg_per_m": "6",
    "reserve_fraction_bp": "3000",
    "alternate_fraction_bp": "1000",
}

#: A third entry that is *not* in the registry: proves a profile must be published,
#: not assumed, and is used by the tests that load a config-release row.
_ORPHAN_VERSION = {**_AIRFRAME_X_SIM, "version": "illustrative-0", "cruise_power_w": "170"}


def _build(mapping: Mapping[str, str]) -> BatteryProfileVersion:
    return BatteryProfileVersion.from_mapping(mapping)


AIRFRAME_X_SIM: Final[BatteryProfileVersion] = _build(_AIRFRAME_X_SIM)
AIRFRAME_Y_SIM: Final[BatteryProfileVersion] = _build(_AIRFRAME_Y_SIM)
AIRFRAME_X_SIM_SUPERSEDED: Final[BatteryProfileVersion] = _build(_ORPHAN_VERSION)

_REGISTRY: Final[Mapping[str, BatteryProfileVersion]] = {
    profile.profile_id: profile
    for profile in (AIRFRAME_X_SIM, AIRFRAME_Y_SIM, AIRFRAME_X_SIM_SUPERSEDED)
}


def available_profiles() -> tuple[str, ...]:
    """Every stored profile id, sorted, for an error message or a review."""
    return tuple(sorted(_REGISTRY))


def load_profile(
    name: str, version: str, *, expected_sha256: str | None = None
) -> BatteryProfileVersion:
    """Fetch a named, versioned profile; never fall back to a default.

    A missing profile is an error that lists what *does* exist, because silently
    choosing the closest battery model would put unreviewed constants into a
    dispatch decision.
    """
    profile_id = f"{name}@{version}"
    try:
        profile = _REGISTRY[profile_id]
    except KeyError as error:
        raise UnknownProfileError(
            f"no energy profile {profile_id}; stored profiles: {', '.join(available_profiles())}"
        ) from error
    if expected_sha256 is not None:
        return profile.verify_hash(expected_sha256)
    return profile


def publish_profile(mapping: Mapping[str, str]) -> BatteryProfileVersion:
    """Validate a config-release row and return the profile it describes.

    Publishing is explicit and the result is *not* added to the process registry:
    a run must pass the object (or pin its hash) to use it, so a scenario cannot
    pick up a battery model that nobody reviewed.
    """
    return _build(mapping)
