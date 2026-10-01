"""Shared builders for the energy tests: one calm trace, one 3 km mission.

The numbers are chosen so the tests can assert *relations* between terms rather
than brittle watt-hour literals, while still sitting inside the plausible
Chandigarh synthetic range (3-30 km legs, 250-2000 g payloads, 0-12 m/s wind).
"""

from __future__ import annotations

from droneops_energy import (
    AIRFRAME_X_SIM,
    AIRFRAME_Y_SIM,
    BatteryProfileVersion,
    Leg,
    MissionEnergyRequest,
    Route,
    WindSnapshot,
    publish_profile,
    snapshot_from_rows,
)

#: 2026-10-01T00:00:00Z as integer microseconds from the POSIX epoch, fixed so no
#: test needs a clock.
BASE_US = 1_759_283_200_000_000
MINUTE_US = 60_000_000
KM_MM = 1_000_000

DUE_EAST_MDEG = 90_000
DUE_WEST_MDEG = 270_000


def trace(
    *,
    speed_dmps: int = 0,
    bearing_mdeg: int = DUE_EAST_MDEG,
    gust_dmps: int | None = None,
    minutes: int = 90,
) -> WindSnapshot:
    """A flat frozen trace: one sustained speed and gust for the whole horizon."""
    gust = speed_dmps if gust_dmps is None else max(gust_dmps, speed_dmps)
    rows = [(m, speed_dmps, bearing_mdeg, gust) for m in range(minutes)]
    return snapshot_from_rows(
        rows,
        station="IXC-synthetic",
        source="frozen test trace (no live fetch)",
        bundle_sha256="f" * 64,
        retrieved_at_us=BASE_US,
        expires_at_us=BASE_US + minutes * MINUTE_US,
    )


def route(
    label: str,
    distance_mm: int,
    *,
    course_mdeg: int = DUE_EAST_MDEG,
    climb_mm: int = 0,
    descent_mm: int = 0,
    hover_us: int = 0,
) -> Route:
    """One straight leg, the shape a corridor edge reduces to."""
    return Route(
        label=label,
        legs=(
            Leg(
                distance_mm=distance_mm,
                course_mdeg=course_mdeg,
                climb_mm=climb_mm,
                descent_mm=descent_mm,
                hover_us=hover_us,
            ),
        ),
    )


def handover(*, hover_us: int = 60_000_000) -> Route:
    """Station-keeping only: the land-transfer handover pad wait."""
    return Route(label="handover", legs=(Leg(0, DUE_EAST_MDEG, hover_us=hover_us),))


def mission(
    *,
    distance_mm: int = 3 * KM_MM,
    recovery_mm: int | None = 3 * KM_MM,
    payload_g: int = 500,
    weather: WindSnapshot | None = None,
    profile: BatteryProfileVersion = AIRFRAME_X_SIM,
    charge_bp: int = 10_000,
    soh_bp: int = 10_000,
    hover_us: int = 60_000_000,
    climb_mm: int = 0,
    deadline_us: int | None = None,
    requires_alternate: bool = False,
    clock_us: int = BASE_US,
) -> MissionEnergyRequest:
    """A complete mission: outbound east, handover, recovery back west."""
    recovery = (
        None if recovery_mm is None else route("recovery", recovery_mm, course_mdeg=DUE_WEST_MDEG)
    )
    return MissionEnergyRequest(
        mission_id="ord-test-000",
        payload_g=payload_g,
        outbound=route("outbound", distance_mm, climb_mm=climb_mm),
        handover=handover(hover_us=hover_us),
        recovery=recovery,
        weather=weather if weather is not None else trace(),
        profile=profile,
        charge_bp=charge_bp,
        soh_bp=soh_bp,
        clock_us=clock_us,
        deadline_us=deadline_us,
        requires_alternate=requires_alternate,
    )


def alt_profile(**overrides: str) -> BatteryProfileVersion:
    """Publish a variant profile through the config-release path.

    Nothing is patched in place: a different battery model is a different named
    version with its own hash, exactly as a reviewer would publish one.
    """
    mapping = dict(AIRFRAME_X_SIM.to_mapping())
    mapping.update(overrides)
    mapping["name"] = overrides.get("name", "airframe-x-variant")
    mapping["version"] = overrides.get("version", "illustrative-1")
    return publish_profile(mapping)


__all__ = [
    "AIRFRAME_Y_SIM",
    "BASE_US",
    "DUE_EAST_MDEG",
    "DUE_WEST_MDEG",
    "KM_MM",
    "MINUTE_US",
    "alt_profile",
    "handover",
    "mission",
    "route",
    "trace",
]
