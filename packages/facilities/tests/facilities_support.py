"""Shared fixtures for the facilities tests.

Numbers are deliberately small and whole: one chain of 80 seconds, one minute a
60-second step. Every duration below is an experiment parameter chosen so a
saturation boundary is visible in a single assertion, not a reproduction of a
measured turnaround.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import yaml

from droneops_contracts.facilities import FacilityStatus, HandoverType
from droneops_facilities import (
    Accessibility,
    CapacityProfile,
    ClaimPurpose,
    OutageWindow,
    ResourceClaim,
    TurnaroundDurations,
    TurnaroundRequest,
)
from droneops_facilities.resources import UnitResource
from droneops_geom_core import MICROS_PER_MINUTE, Interval, ResourceKind

SECOND_US = 1_000_000
MINUTE_US = MICROS_PER_MINUTE

REPO_ROOT = Path(__file__).resolve().parents[3]
BUNDLE_DIR = REPO_ROOT / "simulation" / "scenarios" / "chandigarh-synthetic-v1"

POLICY = "sim-facility-policy-v1"

# 10 + 10 approach/landing, 20 handover, 30 loading, 10 departure = 80 s chain,
# with a 50 s crewed span starting 20 s into the claim.
QUICK_TURNAROUND = TurnaroundDurations(
    approach_us=10 * SECOND_US,
    landing_us=10 * SECOND_US,
    handover_us=20 * SECOND_US,
    loading_us=30 * SECOND_US,
    departure_us=10 * SECOND_US,
)
CHAIN_US = QUICK_TURNAROUND.total_us
CREW_US = QUICK_TURNAROUND.crew_us
PRE_CREW_US = QUICK_TURNAROUND.pre_crew_us

HORIZON_MIN = 60


def make_profile(
    facility_id: str = "hub-test",
    *,
    pads: int = 2,
    charger_bays: int = 2,
    crew_slots: int = 1,
    opens_min: int = 0,
    closes_min: int = HORIZON_MIN,
    crew_shifts: tuple[tuple[int, int], ...] = ((0, HORIZON_MIN),),
    outages: tuple[OutageWindow, ...] = (),
    status: FacilityStatus = FacilityStatus.ACTIVE,
    requires_crew: bool = True,
    step_free: bool = True,
    policy_version: str = POLICY,
    durations: TurnaroundDurations = QUICK_TURNAROUND,
    recharge_note_w: int = 1200,
) -> CapacityProfile:
    """A site with the capacity a test wants and nothing else."""
    return CapacityProfile(
        facility_id=facility_id,
        kind="hub",
        name=f"Test Site {facility_id}",
        status=status,
        opens_us=opens_min * MINUTE_US,
        closes_us=closes_min * MINUTE_US,
        pad_count=pads,
        charger_bays=charger_bays,
        crew_slots=crew_slots,
        turnaround=durations,
        accessibility=Accessibility(
            handover_type=HandoverType.LAND_TRANSFER,
            step_free=step_free,
            ground_pickup=True,
            requires_crew=requires_crew,
        ),
        charge_power_w=recharge_note_w,
        crew_shifts=tuple(Interval.of(low * MINUTE_US, high * MINUTE_US) for low, high in crew_shifts),
        outages=outages,
        policy_version=policy_version,
    )


def make_request(
    request_id: str,
    facility_id: str = "hub-test",
    *,
    ready_s: int = 0,
    deadline_s: int = HORIZON_MIN * 60,
    recharge_us: int = 0,
    preference: int = 0,
    allow_queue: bool = True,
    requires_recharge: bool | None = None,
    durations: TurnaroundDurations | None = None,
    policy_version: str = POLICY,
    holder: str | None = None,
) -> TurnaroundRequest:
    """An arrival. Recharge is off unless a duration is given, so a test that
    wants a pad bottleneck is not accidentally testing a bay bottleneck too."""
    return TurnaroundRequest(
        request_id=request_id,
        facility_id=facility_id,
        holder=holder or f"aircraft-{request_id}",
        ready_us=ready_s * SECOND_US,
        deadline_us=deadline_s * SECOND_US,
        policy_version=policy_version,
        durations=durations,
        requires_recharge=(recharge_us > 0) if requires_recharge is None else requires_recharge,
        recharge_us=recharge_us,
        preference=preference,
        allow_queue=allow_queue,
    )


def make_claim(
    request_id: str,
    *,
    facility_id: str = "hub-test",
    kind: ResourceKind = ResourceKind.PAD,
    index: int = 0,
    start_s: int,
    end_s: int,
) -> ResourceClaim:
    """A claim as if the ledger already held it."""
    prefix = {ResourceKind.PAD: "pad", ResourceKind.CHARGER_BAY: "charger", ResourceKind.CREW: "crew"}[kind]
    purpose = {
        ResourceKind.PAD: ClaimPurpose.PAD,
        ResourceKind.CHARGER_BAY: ClaimPurpose.CHARGER,
        ResourceKind.CREW: ClaimPurpose.CREW,
    }[kind]
    return ResourceClaim(
        request_id=request_id,
        holder=f"aircraft-{request_id}",
        facility_id=facility_id,
        unit=UnitResource(kind, facility_id, index, prefix),
        window=Interval.of(start_s * SECOND_US, end_s * SECOND_US),
        purpose=purpose,
    )


def load_bundle_document(name: str = "facilities.yaml") -> Mapping[str, object]:
    """Read the real synthetic bundle: the tests must schedule the shipped data."""
    path = BUNDLE_DIR / name
    if not path.is_file():
        raise AssertionError(f"missing scenario document {path}")
    parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(parsed, Mapping):
        raise AssertionError(f"{path} did not parse to a mapping")
    return {str(key): value for key, value in parsed.items()}
