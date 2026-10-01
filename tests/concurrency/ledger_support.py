"""Shared ledger builders and world seeding for the concurrency proofs.

Named support module, not ``conftest``: pytest runs in prepend mode, so ``conftest`` is one
flat module name repo-wide and the first one loaded wins. ``tests/unit/test_module_layout.py``
enforces that no test module imports shared builders by that flat name; see the
``energy_support.py`` / ``airspace_support.py`` / ``gis_etl_support.py`` precedent.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from droneops_conflict_checker import CheckerPolicy
from droneops_contracts import AircraftStatus, AltitudeReference, LinkQuality, PriorityClass
from droneops_geom_core.intervals import Interval
from droneops_geom_core.resources import resource_key
from droneops_platform.ports import TenantContext
from droneops_reservations.commit import AllowRule, FaultProgram, FinalSet, ValidationOutcome
from droneops_reservations.intake import (
    AircraftCandidate,
    CandidatePlan,
    CommitCommand,
    EnergyVerdict,
    ProfileSample,
    Verdict,
    WorldView,
)
from droneops_reservations.protocol import CommitStep, ResourceClaim
from droneops_reservations.ledger import LedgerStore, ReservationCommitLedger
from droneops_reservations.ledger.validators import IntervalCommitValidator

#: Fixed, future, timezone-aware. A proof must not change meaning depending on the day it
#: is run, and none of these windows can collide with anything real.
BASE = datetime(2027, 3, 4, 6, 0, tzinfo=UTC)
SLOT = timedelta(minutes=10)

@dataclass(frozen=True)
class World:
    """One seeded tenant's slice of the world: two lanes, two cells, one order."""

    tenant_id: uuid.UUID
    order_id: uuid.UUID
    idempotency_key: str
    config_release_sha: str
    policy_version: str
    origin_facility_id: uuid.UUID
    destination_facility_id: uuid.UUID
    corridor: str = "C-01"
    other_corridor: str = "C-02"
    volume_cell: str = "V-01"
    other_volume_cell: str = "V-02"
    resources: tuple[str, ...] = ()
    adjacency: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def context(self, actor: str = "scheduler") -> TenantContext:
        return TenantContext(
            tenant_id=self.tenant_id,
            actor=actor,
            config_release_sha=self.config_release_sha,
            request_id=str(uuid.uuid4()),
        )


def _hex64() -> str:
    return uuid.uuid4().hex + uuid.uuid4().hex


SOURCE_RELEASE = "sha256:" + "a" * 64


async def seed_world(
    store: LedgerStore,
    policy: CheckerPolicy,
    *,
    corridors: Sequence[str] = ("C-01", "C-02"),
    cells: Sequence[str] = ("V-01", "V-02"),
    adjacency: dict[str, tuple[str, ...]] | None = None,
    restricted: tuple[str, ...] = (),
    unavailable: tuple[str, ...] = (),
) -> World:
    """Insert one tenant's world, including the resource rows the ledger locks.

    ``resource_catalog`` gets one row per *physical* resource (kind + identifier, no
    window) because that is the row step 7 locks; reservation rows carry the windowed key.
    ``config_release`` is seeded too - step 2 refuses to commit against a release it cannot
    read, and that refusal is the point rather than an inconvenience.
    """
    tenant_id = uuid.uuid4()
    order_id = uuid.uuid4()
    facilities = (uuid.uuid4(), uuid.uuid4())
    idempotency_key = f"request-{uuid.uuid4()}"
    sha = "sha256:" + (uuid.uuid4().hex + uuid.uuid4().hex)[:64]
    identities = tuple(f"corridor:{corridor}" for corridor in corridors) + tuple(
        f"volume_cell:{cell}" for cell in cells
    )
    async with store.transaction(tenant_id) as tx:
        await tx.execute(
            "insert into tenant (id, name, data_region, status) values ($1, $2, 'in-central', 'active')",
            tenant_id,
            f"ledger-test-{tenant_id.hex[:8]}",
        )
        for facility_id, name in zip(facilities, ("hub-north", "node-south"), strict=True):
            await tx.execute(
                "insert into facility (id, tenant_id, name, kind, source) "
                "values ($1, $2, $3, 'hub', 'ledger-test')",
                facility_id,
                tenant_id,
                name,
            )
        await tx.execute(
            "insert into config_release (id, tenant_id, config_release_sha, label, policy_version, "
            "protected_volume_json) values ($1, $2, $3, $4, $5, $6::jsonb)",
            uuid.uuid4(),
            tenant_id,
            sha.removeprefix("sha256:"),
            "ledger-test-release",
            policy.policy_version,
            json.dumps(
                {
                    "schema_version": policy.schema_version,
                    "policy_version": policy.policy_version,
                    "illustrative": True,
                    "altitude_reference": policy.altitude_reference.value,
                    "horizontal_margin_m": policy.horizontal_margin_mm / 1000,
                    "vertical_margin_m": policy.vertical_margin_mm / 1000,
                },
                sort_keys=True,
            ),
        )
        await _insert_order(tx, order_id, tenant_id, facilities, idempotency_key, sha)
        for corridor in corridors:
            await tx.execute(
                "insert into corridor (id, tenant_id, corridor_key, origin_facility_id, "
                "destination_facility_id, source_release, status, synthetic, requires_survey, "
                "floor_altitude_m, floor_altitude_reference, ceiling_altitude_m, "
                "ceiling_altitude_reference) "
                "values ($1, $2, $3, $4, $5, $6, $7::corridor_status, true, true, 0, "
                "'AGL', 120, 'AGL')",
                uuid.uuid4(),
                tenant_id,
                f"corridor:{corridor}",
                facilities[0],
                facilities[1],
                SOURCE_RELEASE,
                "restricted" if corridor in restricted else "open",
            )
        for identity in identities:
            status = "outage" if identity in unavailable else "available"
            await tx.execute(
                "insert into resource_catalog (resource_key, kind, tenant_id, capacity, status, "
                "source_release, label) values ($1, $2::resource_kind, $3, 1, $4, $5, $6)",
                identity,
                identity.partition(":")[0],
                tenant_id,
                status,
                SOURCE_RELEASE,
                identity,
            )
    return World(
        tenant_id=tenant_id,
        order_id=order_id,
        idempotency_key=idempotency_key,
        config_release_sha=sha,
        policy_version=policy.policy_version,
        origin_facility_id=facilities[0],
        destination_facility_id=facilities[1],
        resources=identities,
        adjacency=dict(adjacency or {}),
    )


async def _insert_order(
    tx: object,
    order_id: uuid.UUID,
    tenant_id: uuid.UUID,
    facilities: tuple[uuid.UUID, ...],
    idempotency_key: str,
    sha: str,
) -> None:
    await tx.execute(  # type: ignore[attr-defined]
        'insert into "order" (id, tenant_id, external_ref_hash, origin_facility_id, '
        "destination_facility_id, payload_profile_id, priority_class, earliest_departure, "
        "requested_deadline, idempotency_key, config_release_sha, status) "
        "values ($1, $2, $3, $4, $5, 'pp-light', 'routine', $6, $7, $8, $9, 'validated')",
        order_id,
        tenant_id,
        _hex64(),
        facilities[0],
        facilities[1],
        BASE - timedelta(hours=1),
        BASE + timedelta(hours=2),
        idempotency_key,
        sha,
    )


async def add_order(store: LedgerStore, world: World) -> tuple[uuid.UUID, str]:
    """A second order in the same tenant, for identity-binding proofs."""
    order_id = uuid.uuid4()
    key = f"request-{uuid.uuid4()}"
    async with store.transaction(world.tenant_id) as tx:
        await _insert_order(
            tx, order_id, world.tenant_id, (world.origin_facility_id, world.destination_facility_id), key,
            world.config_release_sha,
        )
    return order_id, key


# -------------------------------------------------------------------------- builders
async def add_orders(store: LedgerStore, world: World, count: int) -> list[tuple[uuid.UUID, str]]:
    """``count`` further orders in the same tenant.

    ``mission`` carries ``unique (tenant_id, order_id)`` - one mission per order is the
    schema's half of idempotency - so N simultaneous *commitments* are N demands, each with
    its own order and idempotency key. Reusing one order would prove only that the database
    can refuse a duplicate, not that the ledger can arbitrate contention.
    """
    created: list[tuple[uuid.UUID, str]] = []
    async with store.transaction(world.tenant_id) as tx:
        for _ in range(count):
            order_id = uuid.uuid4()
            key = f"request-{uuid.uuid4()}"
            await _insert_order(
                tx,
                order_id,
                world.tenant_id,
                (world.origin_facility_id, world.destination_facility_id),
                key,
                world.config_release_sha,
            )
            created.append((order_id, key))
    return created


def window(slot: int, *, length_minutes: int = 5) -> Interval:
    """A half-open microsecond window, ``length_minutes`` long, in slot ``slot``."""
    start = BASE + slot * SLOT
    return Interval.from_datetimes(start, start + timedelta(minutes=length_minutes))


def corridor_claim(corridor: str, slot: int, *, length_minutes: int = 5) -> ResourceClaim:
    return ResourceClaim(resource_key("corridor", corridor, None), window(slot, length_minutes=length_minutes))


def cell_claim(cell: str, slot: int, *, length_minutes: int = 5) -> ResourceClaim:
    span = window(slot, length_minutes=length_minutes)
    return ResourceClaim(resource_key("volume_cell", cell, span), span)


def profile(
    slot: int,
    *,
    length_minutes: int = 5,
    lat: float = 30.75,
    lon: float = 76.78,
    altitude_m: float = 60.0,
    reference: AltitudeReference = AltitudeReference.AGL,
) -> tuple[ProfileSample, ...]:
    """Two raw plan points across the window - the payload a checker re-derives from."""
    span = window(slot, length_minutes=length_minutes)
    start, end = span.as_datetimes()
    return (
        ProfileSample(start, lat, lon, altitude_m, reference, 12_000),
        ProfileSample(end, lat + 0.02, lon + 0.02, altitude_m, reference, 12_000),
    )


def energy(margin_wh: int = 90, *, reserve_wh: int = 30, recovery: bool = True) -> EnergyVerdict:
    return EnergyVerdict(
        available_wh=200,
        consumed_wh=200 - margin_wh - reserve_wh,
        reserve_floor_wh=reserve_wh,
        recovery_margin_wh=margin_wh,
        recovery_feasible=recovery,
        model_version="battery-curve-v1",
    )


def aircraft(
    aircraft_id: str,
    *,
    margin_wh: int = 90,
    status: AircraftStatus = AircraftStatus.AVAILABLE,
    link: LinkQuality = LinkQuality.GOOD,
    lateness_us: int = 0,
    recovery: bool = True,
) -> AircraftCandidate:
    return AircraftCandidate(
        aircraft_id=aircraft_id,
        status=status,
        link=link,
        priority_class=PriorityClass.ROUTINE,
        energy=energy(margin_wh, recovery=recovery),
        lateness_us=lateness_us,
        disturbance_units=0,
        fairness_units=0,
    )


def candidate(
    candidate_id: str,
    aircraft_id: str,
    claims: Sequence[ResourceClaim],
    points: Sequence[ProfileSample],
    *,
    strategy: str = "lane-based",
    planner_verdict: Verdict = Verdict.SAFE,
) -> CandidatePlan:
    return CandidatePlan(
        candidate_id=candidate_id,
        aircraft_id=aircraft_id,
        strategy=strategy,
        route_digest=_hex64(),
        claims=tuple(claims),
        profile=tuple(points),
        planner_verdict=planner_verdict,
    )


def command(
    world: World,
    token: str,
    *,
    claims: Sequence[ResourceClaim] | None = None,
    slot: int = 0,
    aircraft_candidates: Sequence[AircraftCandidate] | None = None,
    candidates: Sequence[CandidatePlan] | None = None,
    allow_rule: tuple[str, str] | None = None,
    expected_version: int | None = None,
    order_id: uuid.UUID | None = None,
    idempotency_key: str | None = None,
) -> CommitCommand:
    """A complete, self-consistent commit attempt against the seeded world."""
    resolved_claims = tuple(claims if claims is not None else (corridor_claim(world.corridor, slot),))
    planes = (aircraft("AC-001"),) if aircraft_candidates is None else tuple(aircraft_candidates)
    plans = (
        (candidate(f"cand-{token}", planes[0].aircraft_id, resolved_claims, profile(slot)),)
        if candidates is None
        else tuple(candidates)
    )
    return CommitCommand(
        tenant_id=world.tenant_id,
        order_id=order_id if order_id is not None else world.order_id,
        mission_id=uuid.uuid5(uuid.NAMESPACE_OID, token),
        plan_version_id=uuid.uuid5(uuid.NAMESPACE_OID, f"{token}:plan"),
        commitment_token=token,
        idempotency_key=idempotency_key or world.idempotency_key,
        policy_version=world.policy_version,
        config_release_sha=world.config_release_sha,
        actor="ledger-test",
        world=WorldView(
            policy_version=world.policy_version,
            config_release_sha=world.config_release_sha,
            snapshot_digest="",
            reservation_version=0,
        ),
        aircraft=planes,
        candidates=plans,
        expected_reservation_version=expected_version,
        request_id=str(uuid.uuid4()),
        allow_rule_id=None if allow_rule is None else allow_rule[0],
        allow_rule_version=None if allow_rule is None else allow_rule[1],
    )


def outcome(
    verdict: Verdict,
    reasons: tuple[str, ...] = (),
    grant: tuple[str, str] | None = None,
) -> ValidationOutcome:
    return ValidationOutcome(
        verdict=verdict,
        reasons=reasons,
        grant=None if grant is None else AllowRule(grant[0], grant[1]),
        checker_name="stub-validator",
        checker_version="0",
        records_checked=1,
        pairs_checked=1,
    )


def make_ledger(
    store: LedgerStore,
    validator: object | None = None,
    *,
    fail_at: CommitStep | None = None,
) -> ReservationCommitLedger:
    """A ledger around one validator, with the event clock fixed for reproducibility."""
    resolved = validator if validator is not None else IntervalCommitValidator()
    vote: Callable[[FinalSet], ValidationOutcome] = resolved.validate  # type: ignore[attr-defined]
    return ReservationCommitLedger(
        store,
        vote,
        clock=lambda: BASE,
        fault=FaultProgram(fail_at) if fail_at is not None else None,
    )


