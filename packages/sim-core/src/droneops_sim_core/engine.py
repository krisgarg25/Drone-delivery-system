"""The Phase 1 deterministic simulation engine.

Fixed-step world clock in integer microseconds, one single-threaded event queue, four
seeded noise streams and capacity that queues work instead of overbooking it. The engine
consumes a frozen `WorldSpec` of pure value objects and produces an ordered event log,
periodic `WorldSnapshot`s and a terminal metric vector in which every number carries its
formula and denominator. No I/O, no wall clock, no network, no unsorted iteration: a run
of the same spec on the same seed is byte-identical in any process.

Dispatch rules enforced here (05-chandigarh-demo.md section 5.7):
- a mission is never assigned to an aircraft that cannot complete recovery with reserve;
- offline and maintenance aircraft never appear eligible;
- full pads and charger bays create queueing, not overbooking;
- duplicate submissions produce one mission and one usage record;
- lost telemetry produces an uncertainty state, never a release of airspace;
- impossible orders get an explicit rejection with a reason code; rejected work is
  counted and reported, never hidden.
"""

from __future__ import annotations

import heapq
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from droneops_sim_core.model import (
    AircraftProfile,
    Closure,
    CorridorEdge,
    Facility,
    Fault,
    Order,
    RunMetric,
    WeatherTracePoint,
    WorldSnapshot,
    WorldSpec,
)
from droneops_sim_core.streams import DeterministicStreams

US = 1_000_000
MINUTE_US = 60 * US
HOUR_US = 3600 * US
REJECT_REASONS = (
    "crew_unavailable",
    "duplicate_submission",
    "energy_reserve_unmet",
    "facility_closed",
    "no_capacity_before_deadline",
    "no_eligible_aircraft",
    "no_route",
    "payload_exceeds_profile",
    "reservation_conflict",
    "weather_window_closed",
)
STALE_STATE = "protected_pending_reconciliation"


@dataclass(frozen=True)
class EngineResult:
    events: tuple[dict[str, Any], ...]
    metrics: tuple[RunMetric, ...]
    metric_vector: dict[str, Any]
    snapshots: tuple[WorldSnapshot, ...]


@dataclass
class _AircraftState:
    aircraft_id: str
    profile: AircraftProfile
    facility_id: str
    status: str
    charge_wh: int
    capacity_wh: int
    reposition_enabled: bool
    home_facility_id: str
    mission_order_id: str | None = None


@dataclass
class _LedgerEntry:
    resource_key: str
    start_us: int
    end_us: int
    order_id: str
    void: bool = False


def _overlaps(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    """Half-open integer interval overlap: the conflict kernel, no datetime involved."""
    return a_start < b_end and b_start < a_end


class Engine:
    """One deterministic run of one frozen world. Single thread, single clock."""

    def __init__(self, spec: WorldSpec) -> None:
        from droneops_sim_core import DeterministicEventQueue  # noqa: PLC0415
        # Deferred import: the package root re-exports Engine, so a module-level
        # import here would be a cycle. The one deliberate late import in this file.

        self.spec = spec
        self.queue = DeterministicEventQueue()
        self.streams = DeterministicStreams(spec.seed)
        self.events: list[dict[str, Any]] = []
        self.counters: dict[str, int] = {}
        self.snapshots: list[WorldSnapshot] = []
        self.air: dict[str, _AircraftState] = {}
        self.pad_free: dict[str, list[int]] = {}
        self.charger_free: dict[str, list[int]] = {}
        self.ledger: list[_LedgerEntry] = []
        self.committed_log: list[_LedgerEntry] = []
        self.seen_orders: set[str] = set()
        self.mission_of_order: dict[str, str] = {}
        self.deferred_orders: set[str] = set()
        self.failed_orders: set[str] = set()
        self.durations_us: list[int] = []
        self.pad_waits_us: list[int] = []
        self.charger_waits_us: list[int] = []
        self.queue_ages_us: list[int] = []
        self.energy_wh_total = 0
        self.mission_energy_wh_total = 0
        self.reposition_energy_wh = 0

    # ------------------------------------------------------------- emission
    def emit(self, at_us: int, kind: str, **fields: Any) -> None:
        self.events.append({"at_us": at_us, "kind": kind, "seq": len(self.events), **fields})

    def _count(self, key: str, amount: int = 1) -> None:
        self.counters[key] = self.counters.get(key, 0) + amount

    # ------------------------------------------------------------- world queries
    def _wind_ms(self, at_us: int) -> int:
        trace: tuple[WeatherTracePoint, ...] = self.spec.wind_trace
        minute = at_us // MINUTE_US
        base = trace[0].wind_ms
        for point in trace:
            if point.minute <= minute:
                base = point.wind_ms
            else:
                break
        return max(
            0, base + self.streams.wind_delta_ms(self.spec.policies.get("wind_jitter_ms", 0))
        )

    def _closure_blocker(self, corridor_id: str, start_us: int, end_us: int) -> Closure | None:
        buffer_us = self.spec.policies["separation_buffer_us"]
        for closure in self.spec.closures:
            if closure.corridor_id != corridor_id:
                continue
            if _overlaps(start_us - buffer_us, end_us + buffer_us, closure.from_us, closure.to_us):
                return closure
        return None

    def _edge_speed_mps(self, edge: CorridorEdge) -> int:
        band_cap = {"A": 14, "B": 16, "C": 18}.get(edge.band, 16)
        cap = self.spec.policies.get("lane_speed_cap_mps", band_cap)
        return max(1, min(band_cap, cap))

    def _segments(
        self, path: list[CorridorEdge], depart_us: int
    ) -> list[tuple[CorridorEdge, int, int]]:
        segments: list[tuple[CorridorEdge, int, int]] = []
        cursor = depart_us
        buffer_us = self.spec.policies["separation_buffer_us"]
        for edge in path:
            travel_us = (edge.length_m * US) // self._edge_speed_mps(edge)
            segments.append((edge, cursor, cursor + travel_us + buffer_us))
            cursor = segments[-1][2]
        return segments

    def _route(self, origin: str, dest: str) -> list[CorridorEdge] | None:
        """Deterministic Dijkstra on directed lane length; ties broken by edge id."""
        adjacency: dict[str, list[CorridorEdge]] = {}
        for corridor_id in sorted(self.spec.edges):
            edge = self.spec.edges[corridor_id]
            if edge.ceiling_m_agl > self.spec.policies["max_agl_m"]:
                continue
            adjacency.setdefault(edge.from_id, []).append(edge)
        best: dict[str, tuple[int, str]] = {origin: (0, "")}
        prev: dict[str, str] = {}
        heap: list[tuple[int, str]] = [(0, origin)]
        visited: set[str] = set()
        while heap:
            cost, node = heapq.heappop(heap)
            if node in visited:
                continue
            visited.add(node)
            for edge in sorted(adjacency.get(node, []), key=lambda row: row.corridor_id):
                candidate = (cost + edge.length_m, edge.corridor_id)
                if edge.to_id not in best or candidate < best[edge.to_id]:
                    best[edge.to_id] = candidate
                    prev[edge.to_id] = edge.corridor_id
                    heapq.heappush(heap, (candidate[0], edge.to_id))
        if dest not in visited:
            return None
        path: list[CorridorEdge] = []
        node = dest
        while node in prev:
            edge = self.spec.edges[prev[node]]
            path.append(edge)
            node = edge.from_id
        path.reverse()
        return path

    # ------------------------------------------------------------- capacity slots
    def _pad_earliest(self, facility: Facility, earliest_us: int, duration_us: int) -> int | None:
        """Earliest feasible pad start without consuming it; None if never in horizon.

        Honours pad capacity (queue, never overbook), opening hours, crew shifts and
        outage windows. The caller commits with `_take_pad` only after every other
        feasibility check has passed.
        """
        pads = self.pad_free[facility.facility_id]
        open_start, open_end = facility.open_us(self.spec.horizon_us)
        start = max(earliest_us, min(pads), open_start)
        for _ in range(128):  # bounded scan across outage and crew windows
            if start + duration_us > open_end or start > self.spec.horizon_us:
                return None
            if facility.outage_at(start):
                start = max(start, facility.outage_to_us)
                continue
            if not facility.crew_open_at(start):
                nxt = self._next_crew_time(facility, start)
                if nxt is None or nxt <= start:
                    return None
                start = nxt
                continue
            if any(row <= start for row in pads):
                return start
            start = min(pads) + US
        return None

    def _take_pad(self, facility: Facility, start_us: int, duration_us: int) -> None:
        pads = self.pad_free[facility.facility_id]
        idx = min(range(len(pads)), key=lambda row: (pads[row], row))
        pads[idx] = start_us + duration_us

    def _next_crew_time(self, facility: Facility, at_us: int) -> int | None:
        minute = at_us // MINUTE_US
        for shift_start, shift_end in sorted(facility.crew_shifts):
            if shift_start > minute:
                return shift_start * MINUTE_US
            if shift_start <= minute < shift_end:
                return at_us
        return None

    def _charger_slot(
        self, facility: Facility, earliest_us: int, duration_us: int
    ) -> tuple[int, int]:
        bays = self.charger_free[facility.facility_id]
        idx = min(range(len(bays)), key=lambda row: (bays[row], row))
        start = max(earliest_us, bays[idx])
        bays[idx] = start + duration_us
        return start, max(0, start - earliest_us)

    # ------------------------------------------------------------- energy model
    def _mission_energy_wh(
        self, path: list[CorridorEdge], payload_g: int, profile: AircraftProfile, depart_us: int
    ) -> int:
        """Battery curve distance, wind penalty and seeded battery noise; all integer."""
        length_m = sum(edge.length_m for edge in path)
        base_wh = (length_m * profile.wh_per_100km(payload_g)) // 100_000
        wind_bp = 10_000
        for _edge, start, end in self._segments(path, depart_us):
            wind = self._wind_ms((start + end) // 2)
            wind_bp += max(0, wind - 4) * self.spec.policies.get("wind_energy_bp_per_ms", 250)
        wind_bp = min(wind_bp, 16_000)
        noise_bp = self.streams.battery_factor_bp(
            self.spec.policies.get("battery_noise_spread_bp", 300)
        )
        return (base_wh * wind_bp * noise_bp) // 100_000_000

    def _recovery_energy_wh(self, dest: str, profile: AircraftProfile, depart_us: int) -> int:
        """Energy to fly home to the nearest reachable hub: energy the mission must not spend."""
        best: int | None = None
        for facility_id in sorted(self.spec.facilities):
            if self.spec.facilities[facility_id].kind != "hub":
                continue
            path = self._route(dest, facility_id)
            if path is None:
                continue
            energy = self._mission_energy_wh(path, 0, profile, depart_us)
            if best is None or energy < best:
                best = energy
        return best if best is not None else 0

    # ------------------------------------------------------------- eligibility
    def _eligible_at_origin(self, order: Order) -> list[_AircraftState]:
        rows: list[_AircraftState] = []
        for aircraft_id in sorted(self.air):
            air = self.air[aircraft_id]
            if air.status != "available":  # flying/charging/maintenance/offline: never eligible
                continue
            if air.facility_id != order.origin_facility_id:
                continue
            if order.payload_g > air.profile.max_payload_g:
                continue
            rows.append(air)
        return rows

    # ------------------------------------------------------------- order pipeline
    def handle_order(
        self, order: Order, arrival_us: int, first_arrival_us: int, repositioned: bool
    ) -> str:
        if order.order_id in self.seen_orders and not repositioned:
            # Duplicate submission: one mission, one usage record. The duplicate stays
            # visible in the log and in the reason vector; it is never silently dropped.
            self._count("orders_duplicate_rejected")
            self.emit(
                arrival_us, "order.rejected", order_id=order.order_id, reason="duplicate_submission"
            )
            return "duplicate"
        if order.order_id not in self.seen_orders:
            self.seen_orders.add(order.order_id)
            self._count("orders_submitted")
        origin = self.spec.facilities[order.origin_facility_id]
        last_reason = "no_eligible_aircraft"
        for air in sorted(
            self._eligible_at_origin(order), key=lambda row: (-row.charge_wh, row.aircraft_id)
        ):
            outcome, reason = self._try_assign(order, arrival_us, air, origin)
            if outcome == "accepted":
                self._record_age(first_arrival_us, arrival_us)
                return "accepted"
            if outcome == "terminal":
                self._record_age(first_arrival_us, arrival_us)
                self._reject(arrival_us, order, reason)
                return "rejected"
            last_reason = reason
        any_available = any(row.status == "available" for row in self.air.values())
        if not repositioned and any_available and self._begin_reposition(order, arrival_us):
            return "deferred"  # an empty leg will bring capacity to the origin; decide on landing
        self._record_age(first_arrival_us, arrival_us)
        self._reject(arrival_us, order, last_reason)
        return "rejected"

    def _record_age(self, first_arrival_us: int, decision_us: int) -> None:
        self.queue_ages_us.append(max(0, decision_us - first_arrival_us))

    def _reject(self, at_us: int, order: Order, reason: str) -> None:
        self._count("orders_rejected")
        self._count(f"rejected_{reason}")
        self.emit(
            at_us, "order.rejected", order_id=order.order_id, reason=reason, reason_code=reason
        )

    def _try_assign(  # noqa: PLR0911 - this is the decision function; each branch is a reason code
        self, order: Order, arrival_us: int, air: _AircraftState, origin: Facility
    ) -> tuple[str, str]:
        """Returns (outcome, reason): accepted | retry | terminal."""
        path = self._route(order.origin_facility_id, order.destination_facility_id)
        if path is None:
            return "terminal", "no_route"
        energy_wh = self._mission_energy_wh(path, order.payload_g, air.profile, arrival_us)
        recovery_wh = self._recovery_energy_wh(
            order.destination_facility_id, air.profile, arrival_us
        )
        reserve_wh = (air.capacity_wh * air.profile.reserve_fraction_bp) // 10_000
        if energy_wh + recovery_wh + reserve_wh > air.charge_wh:
            return "retry", "energy_reserve_unmet"
        slot_start = self._pad_earliest(origin, arrival_us, origin.loading_turnaround_us)
        if slot_start is None:
            if origin.open_us(self.spec.horizon_us)[1] <= arrival_us:
                return "terminal", "facility_closed"
            return "retry", "no_capacity_before_deadline"
        depart_us = slot_start + origin.loading_turnaround_us
        min_flight_us = sum((edge.length_m * US) // self._edge_speed_mps(edge) for edge in path)
        if depart_us + min_flight_us > order.deadline_us:
            return "retry", "no_capacity_before_deadline"
        segments = self._segments(path, depart_us)
        for edge, seg_start, seg_end in segments:
            if self._closure_blocker(edge.corridor_id, seg_start, seg_end) is not None:
                return "retry", "weather_window_closed"
            if self._wind_ms((seg_start + seg_end) // 2) > edge.max_wind_ms:
                return "retry", "weather_window_closed"
        end_us = segments[-1][2]
        for edge, seg_start, seg_end in segments:
            for entry in self.ledger:
                if entry.resource_key == f"corridor:{edge.corridor_id}" and _overlaps(
                    seg_start, seg_end, entry.start_us, entry.end_us
                ):
                    return "retry", "reservation_conflict"
        self._commit(
            order,
            air,
            path=path,
            segments=segments,
            slot_start_us=slot_start,
            energy_wh=energy_wh,
            end_us=end_us,
            arrival_us=arrival_us,
        )
        return "accepted", ""

    # ------------------------------------------------------------- repositioning
    def _begin_reposition(self, order: Order, arrival_us: int) -> bool:
        """Empty-leg the closest feasible aircraft to the origin; energy is counted."""
        best: tuple[int, str, list[CorridorEdge], int] | None = None
        mission_path = self._route(order.origin_facility_id, order.destination_facility_id)
        if mission_path is None:
            return False
        for aircraft_id in sorted(self.air):
            air = self.air[aircraft_id]
            if air.status != "available" or not air.reposition_enabled:
                continue
            if order.payload_g > air.profile.max_payload_g:
                continue  # cannot fly the payload even after the empty leg: never move it
            if air.facility_id == order.origin_facility_id:
                continue
            path = self._route(air.facility_id, order.origin_facility_id)
            if path is None:
                continue
            flight_us = sum((edge.length_m * US) // self._edge_speed_mps(edge) for edge in path)
            if flight_us > self.spec.policies.get("reposition_max_us", 20 * MINUTE_US):
                continue
            energy_wh = self._mission_energy_wh(path, 0, air.profile, arrival_us)
            reserve_wh = (air.capacity_wh * air.profile.reserve_fraction_bp) // 10_000
            # Pre-check: never burn an empty leg on an order the aircraft then cannot
            # fly. Mission + recovery + reserve must fit in what survives the reposition.
            mission_wh = self._mission_energy_wh(
                mission_path, order.payload_g, air.profile, arrival_us
            )
            recovery_wh = self._recovery_energy_wh(
                order.destination_facility_id, air.profile, arrival_us
            )
            if energy_wh + reserve_wh > air.charge_wh:
                continue
            if mission_wh + recovery_wh + reserve_wh > air.charge_wh - energy_wh:
                continue
            if best is None or (flight_us, aircraft_id) < (best[0], best[1]):
                best = (flight_us, aircraft_id, path, energy_wh)
        if best is None:
            return False
        flight_us, aircraft_id, _path, energy_wh = best
        air = self.air[aircraft_id]
        air.status = "repositioning"
        air.mission_order_id = order.order_id
        air.charge_wh -= energy_wh
        self.deferred_orders.add(order.order_id)
        self.energy_wh_total += energy_wh
        self.reposition_energy_wh += energy_wh
        self._count("repositioning_missions")
        self.emit(
            arrival_us,
            "reposition.committed",
            order_id=order.order_id,
            aircraft_id=aircraft_id,
            from_facility=air.facility_id,
            energy_wh=energy_wh,
        )

        def on_arrive(at_us: int) -> None:
            self.deferred_orders.discard(order.order_id)
            if air.status != "repositioning":
                # Aircraft failed or was diverted mid-leg: the order gets an explicit
                # rejection, never a silent disappearance from the accounting.
                self._reject(at_us, order, "no_eligible_aircraft")
                return
            air.status = "available"
            air.mission_order_id = None
            air.facility_id = order.origin_facility_id
            self.emit(
                at_us, "reposition.complete", order_id=order.order_id, aircraft_id=aircraft_id
            )
            self.handle_order(order, at_us, arrival_us, repositioned=True)

        self.schedule(arrival_us + flight_us, "reposition.complete", on_arrive)
        return True

    # ------------------------------------------------------------- commitment
    def _commit(
        self,
        order: Order,
        air: _AircraftState,
        *,
        path: list[CorridorEdge],
        segments: list[tuple[CorridorEdge, int, int]],
        slot_start_us: int,
        energy_wh: int,
        end_us: int,
        arrival_us: int,
    ) -> None:
        origin = self.spec.facilities[order.origin_facility_id]
        for edge, seg_start, seg_end in segments:
            entry = _LedgerEntry(f"corridor:{edge.corridor_id}", seg_start, seg_end, order.order_id)
            self.ledger.append(entry)
            self.committed_log.append(entry)
        self._take_pad(origin, slot_start_us, origin.loading_turnaround_us)
        air.status = "flying"
        air.mission_order_id = order.order_id
        air.charge_wh -= energy_wh
        self.mission_of_order[order.order_id] = air.aircraft_id
        self.energy_wh_total += energy_wh
        self.mission_energy_wh_total += energy_wh
        self._count("missions_accepted")
        pad_wait = max(0, slot_start_us - arrival_us)
        self.pad_waits_us.append(pad_wait)
        self.emit(
            arrival_us,
            "mission.committed",
            order_id=order.order_id,
            mission_id=f"sim-{order.order_id}",
            aircraft_id=air.aircraft_id,
            corridor_id=path[0].corridor_id,
            path=[edge.corridor_id for edge in path],
            bands=[edge.band for edge in path],
            energy_wh=energy_wh,
            start_us=segments[0][1],
            end_us=end_us,
            pad_wait_us=pad_wait,
            merge_nodes=sorted(
                {edge.to_id for edge in path if edge.to_id in self.spec.merge_nodes}
            ),
        )
        if self.streams.fault_draw() < self.spec.policies.get("link_loss_rate_bp", 0):
            self.schedule(
                (segments[0][1] + end_us) // 2,
                "telemetry.lost",
                self._telemetry_handler(order.order_id),
            )

        def on_land(at_us: int) -> None:
            self._on_land(order, air, at_us)

        self.schedule(end_us, "mission.land", on_land)

    def _telemetry_handler(self, order_id: str) -> Callable[[int], None]:
        def handler(at_us: int) -> None:
            if order_id not in self.mission_of_order or order_id in self.failed_orders:
                self._count("faults_inert")
                self.emit(at_us, "fault.inert", target=order_id, fault_kind="telemetry_loss")
                return
            self._count("telemetry_lost")
            # Lost telemetry becomes uncertainty: the committed protected volume stays
            # held until reconciliation. Uncertainty is never clear airspace.
            self.emit(
                at_us,
                "telemetry.lost",
                order_id=order_id,
                aircraft_id=self.mission_of_order[order_id],
                state=STALE_STATE,
                airspace_released=False,
            )

        return handler

    def _on_land(self, order: Order, air: _AircraftState, landing_us: int) -> None:
        if order.order_id in self.failed_orders:
            return
        destination = self.spec.facilities[order.destination_facility_id]
        self.ledger[:] = [entry for entry in self.ledger if entry.order_id != order.order_id]
        slot_start = self._pad_earliest(destination, landing_us, destination.offload_turnaround_us)
        if slot_start is None:
            dest_wait = 0
            complete_us = landing_us + destination.offload_turnaround_us
        else:
            dest_wait = max(0, slot_start - landing_us)
            self._take_pad(destination, slot_start, destination.offload_turnaround_us)
            complete_us = slot_start + destination.offload_turnaround_us
        self.pad_waits_us.append(dest_wait)
        if dest_wait > 0:
            self.emit(
                landing_us,
                "pad.queue",
                order_id=order.order_id,
                aircraft_id=air.aircraft_id,
                wait_us=dest_wait,
            )

        def on_complete(at_us: int) -> None:
            self._finish_mission(order, air, at_us)

        self.schedule(complete_us, "mission.complete.pending", on_complete)

    def _finish_mission(self, order: Order, air: _AircraftState, complete_us: int) -> None:
        order_id = order.order_id
        if order_id in self.failed_orders or order_id not in self.mission_of_order:
            return
        self.mission_of_order.pop(order_id, None)
        air.mission_order_id = None
        air.facility_id = order.destination_facility_id
        self._count("missions_completed")
        self.durations_us.append(complete_us - order.arrival_us)
        if complete_us > order.deadline_us:
            self._count("missions_late")
            self.emit(
                complete_us,
                "mission.late",
                order_id=order_id,
                late_us=complete_us - order.deadline_us,
            )
        self.emit(complete_us, "mission.complete", order_id=order_id, aircraft_id=air.aircraft_id)
        self._begin_charge_or_release(air, complete_us)

    def _begin_charge_or_release(self, air: _AircraftState, complete_us: int) -> None:
        facility = self.spec.facilities[air.facility_id]
        threshold_wh = (air.capacity_wh * self.spec.charge_ready_pct) // 100
        target_wh = (air.capacity_wh * self.spec.policies.get("charge_target_pct", 90)) // 100
        if air.charge_wh >= threshold_wh:
            air.status = "available"
            return
        if facility.charger_bays == 0:
            air.status = "available"  # grounded uncharged; eligibility checks reject it later
            return
        duration_us = self._charge_duration_us(air, target_wh)
        start_us, wait = self._charger_slot(facility, complete_us, duration_us)
        self.charger_waits_us.append(wait)
        if wait > 0:
            self.emit(complete_us, "charger.queue", aircraft_id=air.aircraft_id, wait_us=wait)
        air.status = "charging"

        def on_charged(at_us: int) -> None:
            air.charge_wh = target_wh
            air.status = "available"
            self.emit(
                at_us, "aircraft.charged", aircraft_id=air.aircraft_id, charge_wh=air.charge_wh
            )

        self.schedule(start_us + duration_us, "aircraft.charged", on_charged)

    def _charge_duration_us(self, air: _AircraftState, target_wh: int) -> int:
        need_wh = max(0, target_wh - air.charge_wh)
        power_w = max(
            1, min(air.profile.charge_power_w, self.spec.facilities[air.facility_id].charge_power_w)
        )
        derate_bp = self.spec.policies.get("charge_derate_bp", 11_000)
        return (need_wh * HOUR_US * derate_bp) // (power_w * 10_000)

    # ------------------------------------------------------------- faults
    def apply_fault(self, fault: Fault, at_us: int) -> None:
        if fault.kind == "telemetry_loss":
            self._telemetry_handler(fault.target_id)(at_us)
            return
        if fault.kind == "aircraft_stall":
            self._stall(fault.target_id, at_us)
            return
        if fault.kind == "weather_window_close":
            self.emit(
                at_us,
                "weather.window_closed",
                corridor_id=fault.target_id,
                until_minute=fault.until_minute,
            )
            return
        if fault.kind == "facility_outage":
            self.emit(
                at_us,
                "facility.outage",
                facility_id=fault.target_id,
                until_minute=fault.until_minute,
            )
            return
        self._count("faults_unknown")

    def _stall(self, aircraft_id: str, at_us: int) -> None:
        air = self.air.get(aircraft_id)
        if air is None or air.status not in {"flying", "repositioning"}:
            self._count("faults_inert")
            self.emit(at_us, "fault.inert", target=aircraft_id, fault_kind="aircraft_stall")
            return
        order_id = air.mission_order_id
        air.status = "maintenance"
        air.mission_order_id = None
        if order_id:
            for entry in self.ledger:
                if entry.order_id == order_id:
                    entry.void = True  # aircraft down: the remaining volume is voided
            self.ledger[:] = [entry for entry in self.ledger if entry.order_id != order_id]
            self.failed_orders.add(order_id)
            self.mission_of_order.pop(order_id, None)
            self._count("missions_failed")
            self.emit(
                at_us,
                "mission.failed",
                order_id=order_id,
                aircraft_id=aircraft_id,
                reason="aircraft_stall",
            )
        until_us = min(
            at_us + self.spec.policies.get("stall_grounding_us", 20 * MINUTE_US),
            self.spec.horizon_us,
        )

        def recover(real_at_us: int) -> None:
            air.status = "available"
            self.emit(real_at_us, "aircraft.maintenance.complete", aircraft_id=aircraft_id)

        self.schedule(until_us, "aircraft.maintenance.complete", recover)

    # ------------------------------------------------------------- snapshots
    def snapshot(self, at_us: int) -> WorldSnapshot:
        aircraft_rows = {
            aircraft_id: {
                "status": self.air[aircraft_id].status,
                "charge_wh": self.air[aircraft_id].charge_wh,
                "facility_id": self.air[aircraft_id].facility_id,
            }
            for aircraft_id in sorted(self.air)
        }
        facility_rows: dict[str, dict[str, Any]] = {}
        for facility_id in sorted(self.spec.facilities):
            pads = self.pad_free[facility_id]
            bays = self.charger_free[facility_id]
            facility_rows[facility_id] = {
                "pads_free": sum(1 for row in pads if row <= at_us),
                "pads_total": len(pads),
                "charger_bays_free": sum(1 for row in bays if row <= at_us),
                "charger_bays_total": len(bays),
                "outage": self.spec.facilities[facility_id].outage_at(at_us),
            }
        snap = WorldSnapshot(
            at_us=at_us,
            seed=self.spec.seed,
            aircraft=aircraft_rows,
            facilities=facility_rows,
            counters=dict(self.counters),
        )
        self.snapshots.append(snap)
        self.emit(
            at_us, "world.snapshot", aircraft=len(aircraft_rows), facilities=len(facility_rows)
        )
        return snap

    # ------------------------------------------------------------- main loop
    def schedule(self, at_us: int, name: str, handler: Callable[[int], None]) -> None:
        self.queue.schedule(at_us, name, handler)

    def run(self) -> EngineResult:
        if self.spec.demand_jitter_us < 0:
            raise ValueError("demand jitter must be non-negative")
        for facility_id in sorted(self.spec.facilities):
            facility = self.spec.facilities[facility_id]
            if facility.pad_count <= 0:
                raise ValueError(f"{facility_id}: a facility needs at least one pad")
            self.pad_free[facility_id] = [0] * facility.pad_count
            self.charger_free[facility_id] = [0] * max(1, facility.charger_bays)
        for cfg in sorted(self.spec.aircraft, key=lambda row: row.aircraft_id):
            profile = self.spec.profiles[cfg.profile_id]
            air = _AircraftState(
                aircraft_id=cfg.aircraft_id,
                profile=profile,
                facility_id=cfg.home_facility_id,
                status=cfg.initial_status,
                charge_wh=(profile.capacity_wh * cfg.charge_pct) // 100,
                capacity_wh=profile.capacity_wh,
                reposition_enabled=cfg.reposition_enabled,
                home_facility_id=cfg.home_facility_id,
            )
            self.air[cfg.aircraft_id] = air
            if cfg.initial_status in {"maintenance", "offline", "charging"}:
                at_us = cfg.available_at_minute * MINUTE_US
                if cfg.initial_status == "charging" and at_us == 0:
                    target_wh = (
                        profile.capacity_wh * self.spec.policies.get("charge_target_pct", 90)
                    ) // 100
                    at_us = min(
                        self.spec.horizon_us,
                        max(MINUTE_US, self._charge_duration_us(air, target_wh)),
                    )

                def backfill(
                    real_at_us: int,
                    air_id: str = cfg.aircraft_id,
                    state: str = cfg.initial_status,
                ) -> None:
                    if state == "charging":
                        ready_wh = (
                            self.air[air_id].capacity_wh
                            * self.spec.policies.get("charge_target_pct", 90)
                        ) // 100
                        self.air[air_id].charge_wh = max(self.air[air_id].charge_wh, ready_wh)
                    self.air[air_id].status = "available"
                    self.emit(
                        real_at_us,
                        "aircraft.available",
                        aircraft_id=air_id,
                        from_state=state,
                    )

                self.schedule(at_us, "aircraft.available", backfill)

        for order in sorted(
            self.spec.orders, key=lambda row: (row.arrival_us, row.priority_rank, row.order_id)
        ):
            arrival_us = order.arrival_us + self.streams.demand_jitter_us(
                self.spec.demand_jitter_us
            )
            self.schedule(arrival_us, "order.arrived", self._arrived_handler(order, arrival_us))

        def fault_handler(row: Fault) -> Callable[[int], None]:
            def handle(at_us: int) -> None:
                self.apply_fault(row, at_us)

            return handle

        for fault in sorted(self.spec.faults, key=lambda row: (row.at_us, row.fault_id)):
            self.schedule(fault.at_us, f"fault.{fault.kind}", fault_handler(fault))

        def snap_tick(at_us: int) -> None:
            self.snapshot(at_us)
            nxt = at_us + self.spec.snapshot_interval_us
            if nxt <= self.spec.horizon_us:
                self.schedule(nxt, "world.snapshot.tick", snap_tick)

        if self.spec.snapshot_interval_us > 0:
            self.schedule(self.spec.snapshot_interval_us, "world.snapshot.tick", snap_tick)
        self.queue.advance(self.spec.horizon_us)
        if not self.snapshots or self.snapshots[-1].at_us != self.spec.horizon_us:
            self.snapshot(self.spec.horizon_us)

        self.counters["orders_unfinished"] = len(self.mission_of_order) + len(self.deferred_orders)
        for order_id in sorted(self.mission_of_order) + sorted(self.deferred_orders):
            self.emit(self.spec.horizon_us, "mission.unfinished_at_cutoff", order_id=order_id)
        invalid = self._verify_ledger()
        self.events.sort(
            key=lambda row: (row["at_us"], row["kind"], str(row.get("order_id", "")), row["seq"])
        )
        vector = self._metric_vector(invalid)
        return EngineResult(
            events=tuple(self.events),
            metrics=self._metric_rows(vector),
            metric_vector=vector,
            snapshots=tuple(self.snapshots),
        )

    def _arrived_handler(self, order: Order, arrival_us: int) -> Callable[[int], None]:
        def handler(at_us: int) -> None:
            self.emit(
                at_us, "order.arrived", order_id=order.order_id, priority_class=order.priority_class
            )
            self.handle_order(order, at_us, arrival_us, repositioned=False)

        return handler

    def _verify_ledger(self) -> int:
        """Independent full-pairwise scan of every volume ever committed: no two
        committed volumes on one resource may overlap. Any hit is a kernel defect."""
        ordered = sorted(
            (entry for entry in self.committed_log if not entry.void),
            key=lambda row: (row.resource_key, row.start_us, row.order_id),
        )
        invalid = 0
        for index, left in enumerate(ordered):
            for right in ordered[index + 1 :]:
                if left.resource_key != right.resource_key:
                    continue
                if _overlaps(left.start_us, left.end_us, right.start_us, right.end_us):
                    invalid += 1
        return invalid

    # ------------------------------------------------------------- metrics
    def _metric_vector(self, invalid: int) -> dict[str, Any]:
        submitted = self.counters.get("orders_submitted", 0)
        completed = self.counters.get("missions_completed", 0)
        rejected = self.counters.get("orders_rejected", 0)
        duplicates = self.counters.get("orders_duplicate_rejected", 0)
        unfinished = self.counters.get("orders_unfinished", 0)
        failed = self.counters.get("missions_failed", 0)
        late = self.counters.get("missions_late", 0)
        vector: dict[str, Any] = {
            "orders_submitted": submitted,
            "orders_requested": submitted,  # legacy Phase 0 key, same meaning
            "missions_accepted": self.counters.get("missions_accepted", 0),
            "missions_completed": completed,
            "completed_missions": completed,  # legacy
            "missions_late": late,
            "missions_failed": failed,
            "orders_unfinished_at_cutoff": unfinished,
            "orders_rejected": rejected,
            "orders_duplicate_rejected": duplicates,
            "unserved": rejected + unfinished + failed,
            "rejection_reasons": {
                key.removeprefix("rejected_"): count
                for key, count in sorted(self.counters.items())
                if key.startswith("rejected_")
            },
            "completion_rate_bp": self._rate_bp(completed, submitted),
            "on_time_rate_bp": self._rate_bp(completed - late, completed),
            "energy_total_wh": self.energy_wh_total,
            "energy_mission_wh": self.mission_energy_wh_total,
            "repositioning_energy_wh": self.reposition_energy_wh,
            "repositioning_missions": self.counters.get("repositioning_missions", 0),
            "energy_per_completion_wh": (self.energy_wh_total // completed) if completed else 0,
            "mean_mission_duration_us": (sum(self.durations_us) // len(self.durations_us))
            if self.durations_us
            else 0,
            "p95_mission_duration_us": self._p95(self.durations_us),
            "mean_pad_wait_us": (sum(self.pad_waits_us) // len(self.pad_waits_us))
            if self.pad_waits_us
            else 0,
            "p95_pad_wait_us": self._p95(self.pad_waits_us),
            "max_pad_wait_us": max(self.pad_waits_us) if self.pad_waits_us else 0,
            "mean_charger_wait_us": (sum(self.charger_waits_us) // len(self.charger_waits_us))
            if self.charger_waits_us
            else 0,
            "p95_charger_wait_us": self._p95(self.charger_waits_us),
            "mean_queue_age_us": (sum(self.queue_ages_us) // len(self.queue_ages_us))
            if self.queue_ages_us
            else 0,
            "p95_queue_age_us": self._p95(self.queue_ages_us),
            "telemetry_lost": self.counters.get("telemetry_lost", 0),
            "faults_inert": self.counters.get("faults_inert", 0),
            "committed_reservations": len(self.committed_log),
            "invalid_committed_overlaps": invalid,
            "events_emitted": len(self.events),
            "authorized_for_flight": False,
        }
        vector.update(self._cost_rows(vector))
        return vector

    def _cost_rows(self, vector: dict[str, Any]) -> dict[str, Any]:
        """Illustrative cost model only; unvalidated and never a price quote."""
        model = self.spec.cost_model
        completed = vector["missions_completed"]
        horizon_minutes = self.spec.horizon_us // MINUTE_US
        hubs = max(1, sum(1 for row in self.spec.facilities.values() if row.kind == "hub"))
        fixed_daily_paise = (
            model.get("hub_cost_per_day_paise", 0) * hubs
            + model.get("aircraft_cost_per_day_paise", 0) * len(self.spec.aircraft)
            + model.get("staff_cost_per_day_paise", 0)
            + model.get("insurance_cost_per_day_paise", 0)
        )
        fixed_paise = (fixed_daily_paise * horizon_minutes) // (24 * 60)
        energy_paise = (
            vector["energy_total_wh"] * model.get("energy_cost_per_kwh_paise", 0)
        ) // 1000
        failed_paise = vector["missions_failed"] * model.get("failed_mission_cost_paise", 0)
        total_paise = fixed_paise + energy_paise + failed_paise
        variable_paise = (energy_paise + failed_paise) // completed if completed else 0
        sensitivity = {
            f"cost_per_delivery_at_{per_day}_per_hub_paise": (fixed_daily_paise // (per_day * hubs))
            + variable_paise
            for per_day in (20, 40, 60)
        }
        return {
            "illustrative_cost_total_paise": total_paise,
            "illustrative_cost_per_delivery_paise": (total_paise // completed) if completed else 0,
            "illustrative_cost_sensitivity_paise": sensitivity,
            "illustrative": True,
        }

    def _rate_bp(self, numerator: int, denominator: int) -> int:
        return (numerator * 10_000) // denominator if denominator else 0

    def _p95(self, rows: list[int]) -> int:
        if not rows:
            return 0
        ordered = sorted(rows)
        index = -(-95 * len(ordered) // 100) - 1
        return ordered[max(0, min(index, len(ordered) - 1))]

    def _metric_rows(self, vector: dict[str, Any]) -> tuple[RunMetric, ...]:
        return (
            RunMetric(
                "orders_submitted",
                vector["orders_submitted"],
                "orders",
                "count of unique order_ids received; duplicates logged separately",
                "n/a",
            ),
            RunMetric(
                "missions_accepted",
                vector["missions_accepted"],
                "missions",
                "count of mission.committed",
                "n/a",
            ),
            RunMetric(
                "missions_completed",
                vector["missions_completed"],
                "missions",
                "count of mission.complete",
                "n/a",
            ),
            RunMetric(
                "missions_late",
                vector["missions_late"],
                "missions",
                "completed after the order deadline",
                "completed missions",
            ),
            RunMetric(
                "missions_failed",
                vector["missions_failed"],
                "missions",
                "in-flight aircraft stalls",
                "n/a",
            ),
            RunMetric(
                "orders_rejected",
                vector["orders_rejected"],
                "orders",
                "count of rejections excluding duplicates, every reason included",
                "submitted orders",
            ),
            RunMetric(
                "orders_duplicate_rejected",
                vector["orders_duplicate_rejected"],
                "orders",
                "duplicate order_id submissions rejected; not counted as submitted work",
                "n/a",
            ),
            RunMetric(
                "orders_unfinished_at_cutoff",
                vector["orders_unfinished_at_cutoff"],
                "orders",
                "accepted, airborne or queued at horizon end",
                "n/a",
            ),
            RunMetric(
                "unserved",
                vector["unserved"],
                "orders",
                "rejected + unfinished at cutoff + failed, each counted once",
                "n/a",
            ),
            RunMetric(
                "rejection_reasons",
                vector["rejection_reasons"],
                "orders/reason",
                "rejections grouped by reason code",
                "n/a",
            ),
            RunMetric(
                "completion_rate_bp",
                vector["completion_rate_bp"],
                "bp",
                "completed / submitted * 10000",
                "submitted orders",
            ),
            RunMetric(
                "on_time_rate_bp",
                vector["on_time_rate_bp"],
                "bp",
                "(completed - late) / completed * 10000",
                "completed missions",
            ),
            RunMetric(
                "energy_total_wh",
                vector["energy_total_wh"],
                "Wh",
                "sum over all missions incl. failed and repositioning legs",
                "n/a",
            ),
            RunMetric(
                "energy_per_completion_wh",
                vector["energy_per_completion_wh"],
                "Wh/order",
                "energy_total_wh / completed",
                "completed missions",
            ),
            RunMetric(
                "repositioning_energy_wh",
                vector["repositioning_energy_wh"],
                "Wh",
                "sum of empty-leg energy",
                "n/a",
            ),
            RunMetric(
                "mean_mission_duration_us",
                vector["mean_mission_duration_us"],
                "us",
                "mean(completion - first arrival)",
                "completed missions",
            ),
            RunMetric(
                "p95_mission_duration_us",
                vector["p95_mission_duration_us"],
                "us",
                "sorted durations at index ceil(0.95*n)-1",
                "completed missions",
            ),
            RunMetric(
                "mean_pad_wait_us",
                vector["mean_pad_wait_us"],
                "us",
                "mean queueing for a pad, origin and destination combined",
                "pad demands observed",
            ),
            RunMetric(
                "p95_pad_wait_us",
                vector["p95_pad_wait_us"],
                "us",
                "p95 of the same pad-wait population",
                "pad demands observed",
            ),
            RunMetric(
                "mean_charger_wait_us",
                vector["mean_charger_wait_us"],
                "us",
                "mean queueing for a charger bay",
                "chargers requested",
            ),
            RunMetric(
                "p95_charger_wait_us",
                vector["p95_charger_wait_us"],
                "us",
                "p95 of the same charger-wait population",
                "chargers requested",
            ),
            RunMetric(
                "mean_queue_age_us",
                vector["mean_queue_age_us"],
                "us",
                "mean arrival-to-decision latency incl. repositioning waits",
                "submitted orders",
            ),
            RunMetric(
                "telemetry_lost",
                vector["telemetry_lost"],
                "events",
                "count of telemetry.lost; volume held as uncertainty, never released",
                "n/a",
            ),
            RunMetric(
                "invalid_committed_overlaps",
                vector["invalid_committed_overlaps"],
                "conflicts",
                "independent pairwise half-open interval scan of committed volumes",
                "gate: must be 0",
            ),
            RunMetric(
                "illustrative_cost_total_paise",
                vector["illustrative_cost_total_paise"],
                "paise",
                "fixed prorated + energy + failed-mission cost - ILLUSTRATIVE",
                "n/a",
            ),
            RunMetric(
                "illustrative_cost_per_delivery_paise",
                vector["illustrative_cost_per_delivery_paise"],
                "paise/delivery",
                "illustrative_cost_total_paise / completed - ILLUSTRATIVE, unvalidated model",
                "completed missions",
            ),
            RunMetric(
                "illustrative_cost_sensitivity_paise",
                vector["illustrative_cost_sensitivity_paise"],
                "paise/delivery",
                "fixed_daily/(deliveries_per_day*hubs) + variable, 20/40/60 per hub - ILLUSTRATIVE",
                "n/a",
            ),
        )
