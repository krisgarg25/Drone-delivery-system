"""Factories for airspace tests: one place to build an honest restriction."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from droneops_airspace import (
    AffectedPlan,
    AltitudeLimit,
    FootprintSpec,
    FreshnessPolicy,
    GeoCoord,
    MissionPhase,
    Restriction,
    RestrictionKind,
    RestrictionSnapshot,
    RouteProfile,
    RouteWaypoint,
    SnapshotAvailability,
    evaluate_route,
)
from droneops_contracts.geometry import AltitudeReference
from droneops_contracts.provenance import Freshness, Provenance

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
"""The fixed clock every test evaluates against: no wall clock, so no flakiness (I7)."""

AIRPORT_LAT_LON = (30.6742097, 76.7895017)
"""Chandigarh aerodrome point as it appears in data/processed/chandigarh-synthetic-v1.

Reused rather than invented. It is an OpenStreetMap ``aeroway`` point, not an official
boundary, which is exactly why the buffer built on it is labelled synthetic.
"""

# A block of the city the corridor model actually covers, as a closed ring.
ZONE_RING: tuple[tuple[float, float], ...] = (
    (30.73000, 76.78000),
    (30.73000, 76.80000),
    (30.75000, 76.80000),
    (30.75000, 76.78000),
    (30.73000, 76.78000),
)

# Crosses the zone diagonally, entering and leaving it.
CROSSING_ROUTE: tuple[tuple[float, float], ...] = (
    (30.72000, 76.77000),
    (30.74000, 76.79000),
    (30.76000, 76.81000),
)

# Stays south-west of the zone throughout.
CLEAR_ROUTE: tuple[tuple[float, float], ...] = (
    (30.70000, 76.75000),
    (30.70500, 76.76000),
    (30.71000, 76.77000),
)

_UNSET = object()


def policy(**kwargs: Any) -> FreshnessPolicy:
    return FreshnessPolicy(**kwargs)


def provenance(
    *,
    source: str = "operator-manual-entry",
    retrieved_at: datetime | None = None,
    expires_at: Any = _UNSET,
    freshness: Freshness = Freshness.FRESH,
) -> Provenance:
    """Fresh by default: a test that breaks trust says so explicitly."""

    moment = NOW - timedelta(hours=1) if retrieved_at is None else retrieved_at
    expiry = NOW + timedelta(hours=12) if expires_at is _UNSET else expires_at
    return Provenance(source=source, retrieved_at=moment, expires_at=expiry, freshness=freshness)


def polygon_footprint(ring: tuple[tuple[float, float], ...] = ZONE_RING) -> FootprintSpec:
    return FootprintSpec(
        shape="polygon", points=tuple(GeoCoord(lat=lat, lon=lon) for lat, lon in ring)
    )


def circle_footprint(*, radius_m: int = 3000) -> FootprintSpec:
    lat, lon = AIRPORT_LAT_LON
    return FootprintSpec(shape="circle", points=(GeoCoord(lat=lat, lon=lon),), radius_m=radius_m)


def buffered_footprint(*, width_m: int = 200) -> FootprintSpec:
    return FootprintSpec(
        shape="buffered_line",
        points=(GeoCoord(lat=30.74, lon=76.76), GeoCoord(lat=30.745, lon=76.78)),
        width_m=width_m,
    )


def restriction(
    *,
    restriction_id: str = "red-1",
    kind: RestrictionKind = RestrictionKind.RED,
    footprint: FootprintSpec | None = None,
    floor_m: float = 0.0,
    ceiling_m: float | None = 120.0,
    altitude_reference: AltitudeReference = AltitudeReference.AGL,
    effective_from: datetime | None = None,
    effective_until: Any = _UNSET,
    source_provenance: Provenance | None = None,
    authoritative: bool = False,
) -> Restriction:
    return Restriction(
        restriction_id=restriction_id,
        kind=kind,
        footprint=footprint or polygon_footprint(),
        altitude=AltitudeLimit(
            floor_m=floor_m, ceiling_m=ceiling_m, altitude_reference=altitude_reference
        ),
        effective_from=effective_from or (NOW - timedelta(hours=2)),
        effective_until=NOW + timedelta(hours=4) if effective_until is _UNSET else effective_until,
        provenance=source_provenance or provenance(),
        authoritative=authoritative,
    )


def untrusted_restriction(
    *,
    retrieved_at: datetime | None = None,
    expires_at: Any = _UNSET,
    freshness: Freshness = Freshness.STALE,
    source: str = "operator-manual-entry",
    **kwargs: Any,
) -> Restriction:
    """A restriction whose data nobody refreshed: the fail-closed subject."""

    kwargs["source_provenance"] = provenance(
        source=source,
        retrieved_at=NOW - timedelta(days=3) if retrieved_at is None else retrieved_at,
        expires_at=NOW - timedelta(days=1) if expires_at is _UNSET else expires_at,
        freshness=freshness,
    )
    return restriction(**kwargs)


def snapshot(*restrictions: Restriction, fetched_at: datetime | None = None) -> RestrictionSnapshot:
    return RestrictionSnapshot(
        snapshot_id="snap-test-1",
        source="operator-manual-entry",
        fetched_at=fetched_at or NOW,
        feed_version="test-v1",
        availability=SnapshotAvailability.MANUAL_OPERATOR_FILE,
        restrictions=tuple(restrictions),
    )


def route(
    points: tuple[tuple[float, float], ...] = CROSSING_ROUTE,
    *,
    plan_id: str = "plan-1",
    altitude_m: float = 80.0,
    planned_start: datetime | None = None,
    segment_seconds: int = 300,
    altitude_reference: AltitudeReference = AltitudeReference.AGL,
) -> RouteProfile:
    waypoints = tuple(
        RouteWaypoint(
            lat=lat,
            lon=lon,
            altitude_m=altitude_m,
            altitude_reference=altitude_reference,
            t_s=index * segment_seconds,
        )
        for index, (lat, lon) in enumerate(points)
    )
    return RouteProfile(
        plan_id=plan_id,
        planned_start_utc=planned_start or NOW,
        waypoints=waypoints,
    )


def AffectedPlanFactory(  # noqa: N802 - used as a factory callable in tests
    phase: MissionPhase,
    plan_id: str,
    points: tuple[tuple[float, float], ...] = CROSSING_ROUTE,
) -> AffectedPlan:
    """One plan, as the change-handling layer sees it."""

    return AffectedPlan(
        plan_id=plan_id,
        mission_id=f"mission-{plan_id}",
        phase=phase,
        route=route(points, plan_id=plan_id),
    )


def evaluate_route_note_probe() -> str:
    """The note attached to a permissive verdict: what an operator would be shown."""

    result = evaluate_route(route(CLEAR_ROUTE), snapshot(), now=NOW, policy=policy())
    return result.note
