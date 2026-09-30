"""Scenario builders shared by the geom-4d test suite.

A module rather than ``conftest``, because every package in this monorepo has a
``conftest`` and only one of them can own that import name during a full-suite
run. The fixtures stay in ``conftest.py``; the builders live here, and a test file
names exactly which scenario vocabulary it is using.

Every profile here is projected on one shared plane at ``ORIGIN``. That is not a
convenience, it is a precondition: two plans whose millimetres come from different
projected origins cannot be compared, and the kernel says ``UNPROVEN`` rather than
inventing a conversion.

Margins and body sizes are experiment parameters, the same kind the policy file
carries; the assertions are about relationships, so retuning a margin changes the
numbers in a scenario without changing which scenarios pass.
"""

from __future__ import annotations

from pathlib import Path

from droneops_geom_4d import ProfilePoint, RouteProfile
from droneops_geom_core import (
    AltitudeReference,
    GeoPoint,
    LocalPlane,
    destination_point,
    metres_to_millimetres,
)

REPO_ROOT = Path(__file__).resolve().parents[3]

#: The one origin every scenario starts from, so bearings and distances compare.
ORIGIN = GeoPoint(30.7387, 76.7687)

#: The single projected frame every profile in the suite shares.
PLANE = LocalPlane(ORIGIN)

TIME_UNIT_US = 1_000_000
AGL = AltitudeReference.AGL


def point_at(bearing_deg: float, distance_m: float, *, from_point: GeoPoint = ORIGIN) -> GeoPoint:
    """A waypoint geodetically at a bearing and distance."""
    return destination_point(from_point, bearing_deg, distance_m)


def waypoint(
    position: GeoPoint,
    *,
    at_us: int,
    altitude_m: float,
    speed_mmps: int,
    reference: AltitudeReference = AGL,
) -> ProfilePoint:
    """A time-indexed point whose altitude carries its frame."""
    return ProfilePoint.at(
        at_us,
        position.lat_deg,
        position.lon_deg,
        metres_to_millimetres(altitude_m),
        reference,
        speed_mmps,
    )


def plan(
    points: list[ProfilePoint],
    *,
    plan_version: str = "plan",
    source: str = "test",
    authoritative: bool = False,
) -> RouteProfile:
    """A profile on the shared plane. Always use this when two plans meet."""
    return RouteProfile.from_points(
        points,
        plan_version=plan_version,
        source=source,
        authoritative=authoritative,
        plane=PLANE,
    )


def straight_profile(
    *,
    distance_m: float,
    bearing_deg: float,
    altitude_m: float = 40.0,
    duration_s: int = 10,
    speed_mmps: int = 10_000,
    start_us: int = 0,
    plan_version: str = "plan-straight",
    source: str = "test",
    reference: AltitudeReference = AGL,
    authoritative: bool = False,
) -> RouteProfile:
    """A two-waypoint straight leg, flown ``duration_s`` seconds after ``start_us``."""
    return plan(
        [
            waypoint(
                ORIGIN,
                at_us=start_us,
                altitude_m=altitude_m,
                speed_mmps=speed_mmps,
                reference=reference,
            ),
            waypoint(
                point_at(bearing_deg, distance_m),
                at_us=start_us + duration_s * TIME_UNIT_US,
                altitude_m=altitude_m,
                speed_mmps=speed_mmps,
                reference=reference,
            ),
        ],
        plan_version=plan_version,
        source=source,
        authoritative=authoritative,
    )


def two_leg_profile(
    *,
    first_m: float,
    second_m: float,
    bearing_deg: float,
    turn_bearing_deg: float,
    start_altitude_m: float,
    end_altitude_m: float,
    duration_s: int = 10,
    speed_mmps: int = 10_000,
    source: str = "test",
) -> RouteProfile:
    """Three waypoints: out, then a turn, climbing between the last two."""
    mid = point_at(bearing_deg, first_m)
    end = point_at(turn_bearing_deg, second_m, from_point=mid)
    return plan(
        [
            waypoint(ORIGIN, at_us=0, altitude_m=start_altitude_m, speed_mmps=speed_mmps),
            waypoint(
                mid,
                at_us=duration_s * TIME_UNIT_US,
                altitude_m=start_altitude_m,
                speed_mmps=speed_mmps,
            ),
            waypoint(
                end,
                at_us=2 * duration_s * TIME_UNIT_US,
                altitude_m=end_altitude_m,
                speed_mmps=speed_mmps,
            ),
        ],
        plan_version="plan-two-leg",
        source=source,
    )
