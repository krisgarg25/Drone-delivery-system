"""Pure Phase 1 world-model data types.

Every object here is a value object: no I/O, no wall clock, no floats where an integer
unit exists (microseconds, millijoules-as-integers, microdegrees). This module is the
shape of the Phase 1 data model (`scenario`, `sim_run`, `world_snapshot`,
`config_release`, `aircraft`, `facility`, `corridor`, `run_metric`); the persistence
agent maps these onto ORM tables. Nothing in this package may import a database, a
network client or a live adapter - `.importlinter` enforces that, not this comment.

Synthetic honesty (docs/conventions.md section 3): every spatial or vertical value
carries its reference (`altitude_reference`, one of AGL/AMSL/GROUND/PAD/UNSPECIFIED)
and a `synthetic` provenance flag. Fictional identifiers only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import pairwise
from typing import Any

ALTITUDE_REFERENCES = ("AGL", "AMSL", "GROUND", "PAD", "UNSPECIFIED")
AIRCRAFT_STATUSES = ("available", "charging", "maintenance", "offline", "repositioning", "flying")
PRIORITY_RANK = {
    "life_critical": 0,
    "safety_contingency": 1,
    "commercial_sla": 2,
    "routine": 3,
}


@dataclass(frozen=True)
class GeometryPoint:
    """A coordinate in integer microdegrees plus an altitude and its explicit reference."""

    lat_e6: int
    lon_e6: int
    alt_m_agl: int
    altitude_reference: str = "AGL"

    def __post_init__(self) -> None:
        if self.altitude_reference not in ALTITUDE_REFERENCES:
            raise ValueError(f"unknown altitude_reference {self.altitude_reference!r}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "lat_e6": self.lat_e6,
            "lon_e6": self.lon_e6,
            "alt_m_agl": self.alt_m_agl,
            "altitude_reference": self.altitude_reference,
            "synthetic": True,
        }


@dataclass(frozen=True)
class CorridorEdge:
    """One directed lane of the corridor graph.

    Opposite directions are separate edges; a merge node is any node shared by more
    than one edge, where entry is metered by the separation buffer.
    """

    corridor_id: str
    from_id: str
    to_id: str
    length_m: int
    width_m: int
    band: str
    floor_m_agl: int
    ceiling_m_agl: int
    max_wind_ms: int
    geometry: tuple[GeometryPoint, ...]
    synthetic: bool = True
    source: str = "synthetic"

    def __post_init__(self) -> None:
        if not self.synthetic:
            raise ValueError("Phase 1 corridor edges are synthetic; authority requires survey")
        for point in self.geometry:
            if point.altitude_reference != "AGL":
                raise ValueError(f"{self.corridor_id}: every corridor altitude must reference AGL")


@dataclass(frozen=True)
class Closure:
    """A timed closure of one directed edge, synthetic and versioned."""

    corridor_id: str
    from_us: int
    to_us: int
    reason: str


@dataclass(frozen=True)
class Facility:
    """Hub or node: pads, charger bays, loading and crew windows, turnaround, outage.

    Capacity is illustrative but enforced: a full pad queues work, it never overbooks.
    """

    facility_id: str
    kind: str  # hub | node
    name: str  # fictional, never a real hospital or business
    location: GeometryPoint
    pad_count: int
    charger_bays: int
    charge_power_w: int
    loading_turnaround_us: int
    offload_turnaround_us: int
    open_minute: int
    close_minute: int
    crew_shifts: tuple[tuple[int, int], ...]
    outage_from_us: int = -1
    outage_to_us: int = -1
    synthetic: bool = True

    def open_us(self, horizon_us: int) -> tuple[int, int]:
        start = self.open_minute * 60 * 1_000_000
        end = self.close_minute * 60 * 1_000_000
        return (start, min(end, horizon_us))

    def crew_open_at(self, at_us: int) -> bool:
        minute = at_us // 60_000_000
        return any(start <= minute < end for start, end in self.crew_shifts)

    def outage_at(self, at_us: int) -> bool:
        return self.outage_from_us <= at_us < self.outage_to_us


@dataclass(frozen=True)
class BatteryCurvePoint:
    payload_g: int
    wh_per_100km: int


@dataclass(frozen=True)
class AircraftProfile:
    """Illustrative aircraft profile with an explicit battery curve.

    The curve is data, not code: linear interpolation between payload points, all
    integer arithmetic. Nothing here is a certified value.
    """

    profile_id: str
    max_payload_g: int
    cruise_speed_mps: int
    capacity_wh: int
    reserve_fraction_bp: int
    charge_power_w: int
    battery_curve: tuple[BatteryCurvePoint, ...]
    source: str = "contracts/vehicle-profile"
    illustrative: bool = True

    def wh_per_100km(self, payload_g: int) -> int:
        if payload_g > self.max_payload_g:
            raise ValueError(
                f"{self.profile_id}: payload {payload_g}g exceeds certified-class limit"
            )
        points = sorted(self.battery_curve, key=lambda row: row.payload_g)
        if payload_g <= points[0].payload_g:
            return points[0].wh_per_100km
        for left, right in pairwise(points):
            if payload_g <= right.payload_g:
                span = right.payload_g - left.payload_g
                if span <= 0:
                    return right.wh_per_100km
                ratio_bp = ((payload_g - left.payload_g) * 10_000) // span
                return (
                    left.wh_per_100km
                    + ((right.wh_per_100km - left.wh_per_100km) * ratio_bp) // 10_000
                )
        return points[-1].wh_per_100km


@dataclass(frozen=True)
class AircraftConfig:
    aircraft_id: str
    profile_id: str
    home_facility_id: str
    initial_status: str
    charge_pct: int
    available_at_minute: int  # for maintenance/offline/charging backfill; 0 = immediately
    reposition_enabled: bool = True

    def __post_init__(self) -> None:
        if self.initial_status not in AIRCRAFT_STATUSES:
            raise ValueError(f"{self.aircraft_id}: unknown initial status {self.initial_status!r}")


@dataclass(frozen=True)
class Order:
    order_id: str
    family: str  # routine | rush | failure
    arrival_minute: int
    origin_facility_id: str
    destination_facility_id: str
    priority_class: str
    payload_g: int
    deadline_minute: int
    partner_id: str = "partner-unspecified-synthetic"

    @property
    def arrival_us(self) -> int:
        return self.arrival_minute * 60 * 1_000_000

    @property
    def deadline_us(self) -> int:
        return self.deadline_minute * 60 * 1_000_000

    @property
    def priority_rank(self) -> int:
        return PRIORITY_RANK.get(self.priority_class, 9)


@dataclass(frozen=True)
class Fault:
    fault_id: str
    kind: str  # telemetry_loss | aircraft_stall | weather_window_close | facility_outage
    at_minute: int
    target_id: str = ""  # order_id, aircraft_id, corridor_id or facility_id
    until_minute: int = 0

    @property
    def at_us(self) -> int:
        return self.at_minute * 60 * 1_000_000


@dataclass(frozen=True)
class WeatherTracePoint:
    minute: int
    wind_ms: int
    headwind_bp: int


@dataclass(frozen=True)
class WorldSpec:
    """The complete frozen world one deterministic run consumes."""

    scenario_id: str
    seed: int
    horizon_us: int
    snapshot_interval_us: int
    policies: dict[str, int]
    profiles: dict[str, AircraftProfile]
    aircraft: tuple[AircraftConfig, ...]
    facilities: dict[str, Facility]
    edges: dict[str, CorridorEdge]
    closures: tuple[Closure, ...]
    merge_nodes: frozenset[str]
    wind_trace: tuple[WeatherTracePoint, ...]
    orders: tuple[Order, ...]
    faults: tuple[Fault, ...]
    demand_jitter_us: int
    charge_ready_pct: int
    cost_model: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class RunMetric:
    """One terminal metric with its formula and denominator. An unexplained number
    is a number nobody can audit; every entry states how it was produced."""

    metric_id: str
    value: int | float | str | bool | dict[str, int]
    unit: str
    formula: str
    denominator: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "metric_id": self.metric_id,
            "value": self.value,
            "unit": self.unit,
            "formula": self.formula,
            "denominator": self.denominator,
        }


@dataclass(frozen=True)
class WorldSnapshot:
    """Serializable truth of the world at one simulation instant."""

    at_us: int
    seed: int
    aircraft: dict[str, dict[str, Any]]
    facilities: dict[str, dict[str, Any]]
    counters: dict[str, int]

    def as_dict(self) -> dict[str, Any]:
        return {
            "at_us": self.at_us,
            "seed": self.seed,
            "aircraft": {key: dict(self.aircraft[key]) for key in sorted(self.aircraft)},
            "facilities": {key: dict(self.facilities[key]) for key in sorted(self.facilities)},
            "counters": {key: self.counters[key] for key in sorted(self.counters)},
        }


@dataclass(frozen=True)
class ScenarioMeta:
    scenario_id: str
    schema_version: int
    authoritative: bool
    seed: int

    def __post_init__(self) -> None:
        if self.authoritative:
            raise ValueError("a Phase 1 scenario is never authoritative")


@dataclass(frozen=True)
class ConfigRelease:
    config_release_sha256: str
    file_count: int


@dataclass(frozen=True)
class SimRunRecord:
    """The persistence-facing shape of one completed run. The persistence agent maps
    this to its tables; sim-core only defines it."""

    identity: dict[str, Any]
    events: tuple[dict[str, Any], ...]
    metrics: tuple[RunMetric, ...]
    snapshots: tuple[WorldSnapshot, ...]
