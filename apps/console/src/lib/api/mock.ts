/**
 * The synthetic dev transport: fixtures shaped by the generated contract types, so a
 * missing or misspelled field is a build error rather than a runtime surprise.
 *
 * It exists because the API needs a running Postgres-backed server (`make db-up &&
 * make api`) and the console must be reviewable without one. It cannot be mistaken for
 * live data, in three independent ways:
 *
 *  1. every response reports `meta.transportMode === "synthetic"`, which the shell prints
 *     in the provenance strip and as a persistent `DEV FIXTURES` badge;
 *  2. every payload's own provenance is `source_kind: "synthetic"`, `authoritative:
 *     false`, so the map draws it with the synthetic channel (dash-dot, diamond, SYNTH);
 *  3. it is refused at construction time in a production build (`import.meta.env.PROD`).
 *
 * The values are modelled on `simulation/golden/chandigarh-basic` and the frozen scenario
 * bundle. They are a demonstration input, not a measurement of anything.
 *
 * Simulation only. Not authorised for flight.
 */

import { SIMULATION_NOTICE } from "./client";
import type {
  ApiResult,
  CallArgs,
  ReadOperationId,
  ResponseMeta,
  Transport,
  TransportMode,
} from "./client";
import type { ResponseFor, SourceKind } from "./types";
import { WORLD, corridorById, corridors, pads, pointAlong } from "../../map/world";

const PROVENANCE = {
  phase: "phase-4-simulation",
  authoritative: false,
  simulation_notice: SIMULATION_NOTICE,
} as const;

function prov(source_kind: SourceKind, authoritative = false) {
  return { ...PROVENANCE, source_kind, authoritative };
}

const TENANT = "22222222-2222-2222-2222-222222222222";
const MISSION = "33333333-3333-3333-3333-333333333333";
const MISSION_B = "34444444-4444-4444-4444-444444444444";
const AIRCRAFT_A = "77777777-7777-7777-7777-777777777771";
const AIRCRAFT_B = "77777777-7777-7777-7777-777777777772";
const AIRCRAFT_C = "77777777-7777-7777-7777-777777777773";
const BASE = "2026-10-01T09:00:00Z";

/** Two known corridors and the two hubs, so the fixtures join onto the real world asset. */
const HUBS = pads().filter((pad) => pad.kind === "hub");
const MAIN = corridorById(corridors()[0].corridor_id);
const CROSS = corridorById(corridors()[1].corridor_id);

if (MAIN === undefined || CROSS === undefined || HUBS.length < 2) {
  throw new Error("the synthetic fixtures need the frozen corridor and hub set");
}

const EDGE_A = MAIN;
const EDGE_B = CROSS;
const HUB_NORTH = HUBS[0];
const HUB_SOUTH = HUBS[1];

/** A walk along one corridor: 8 observations, the last three of them going stale. */
function observations(): ResponseFor<"listMissionObservations">["items"] {
  const items: ResponseFor<"listMissionObservations">["items"] = [];
  const edge = EDGE_A;
  for (let i = 0; i < 8; i += 1) {
    const [lon, lat] = pointAlong(edge, 0.12 + i * 0.09);
    const seconds = i * 20;
    items.push({
      observation_id: `66666666-6666-6666-6666-0000000000${String(i).padStart(2, "0")}`,
      tenant_id: TENANT,
      aircraft_id: AIRCRAFT_A,
      sequence: 400 + i,
      measurement_time: iso(seconds),
      received_time: iso(seconds + 1),
      position: {
        lat: round6(lat),
        lon: round6(lon),
        altitude_m: 62 + i,
        altitude_reference: "AGL",
      },
      out_of_order: i === 6,
      source: "synthetic dev transport (fixtures, not live data)",
      ...prov("observed"),
    });
  }
  return items;
}

function iso(offsetSeconds: number): string {
  return new Date(Date.parse(BASE) + offsetSeconds * 1000).toISOString();
}

function round6(value: number): number {
  return Math.round(value * 1e6) / 1e6;
}

/** The run identity every scenario payload is required to carry. */
function runIdentity(engine: string): ResponseFor<"getSimulationRun">["run_identity"] {
  return {
    bundle_sha256: WORLD.provenance.bundle_sha256,
    engine_version: engine,
    scenario_sha256: WORLD.provenance.bundle_files["corridors.yaml"] ?? "",
    policy_version: WORLD.provenance.policy_version,
    config_release_sha256: WORLD.provenance.config_sha256,
    seed: WORLD.provenance.seed,
    python_version: "3.13",
    machine_architecture: "x86_64",
  };
}

function fixtures(): { [Id in ReadOperationId]: ResponseFor<Id> } {
  return {
    listFacilities: {
      items: pads().map((pad, index) => ({
        facility_id: pad.facility_id,
        tenant_id: TENANT,
        name: `${pad.name} (fictional)`,
        kind: pad.kind,
        status: index === 4 ? "outage" : "active",
        ...(index === 4 ? { outage_reason: "synthetic fixture: pad survey pending" } : {}),
        ...prov("synthetic"),
      })),
      ...prov("synthetic"),
    },
    getFacilityCapacity: {
      facility_id: HUB_NORTH.facility_id,
      pad_capacity: HUB_NORTH.pads,
      pads_committed: 1,
      charger_capacity: HUB_NORTH.charger_bays,
      chargers_committed: 2,
      handover_modes: ["sling", "winch"],
      window_from: iso(0),
      window_to: iso(3600),
      ...prov("synthetic"),
    },
    listAircraft: {
      items: [
        {
          aircraft_id: AIRCRAFT_A,
          tenant_id: TENANT,
          model_id: "dm-v1-synthetic",
          registration_ref: "VT-FIX-001",
          status: "flying",
          link_quality: "degraded",
          base_facility_id: HUB_NORTH.facility_id,
          ...prov("synthetic"),
        },
        {
          aircraft_id: AIRCRAFT_B,
          tenant_id: TENANT,
          model_id: "dm-v1-synthetic",
          registration_ref: "VT-FIX-002",
          status: "charging",
          link_quality: "good",
          base_facility_id: HUB_SOUTH.facility_id,
          ...prov("synthetic"),
        },
        {
          aircraft_id: AIRCRAFT_C,
          tenant_id: TENANT,
          model_id: "dm-v2-synthetic",
          registration_ref: "VT-FIX-003",
          status: "maintenance",
          link_quality: "offline",
          ...prov("synthetic"),
        },
      ],
      ...prov("synthetic"),
    },
    getAircraft: {
      aircraft_id: AIRCRAFT_A,
      tenant_id: TENANT,
      model_id: "dm-v1-synthetic",
      registration_ref: "VT-FIX-001",
      status: "flying",
      link_quality: "degraded",
      base_facility_id: HUB_NORTH.facility_id,
      ...prov("synthetic"),
    },
    listReservations: {
      items: [
        // committed: holds the corridor the aircraft is actually flying
        {
          reservation_id: "55555555-5555-5555-5555-000000000001",
          tenant_id: TENANT,
          mission_id: MISSION,
          resource_key: `corridor:${EDGE_A.corridor_id}:segment-3`,
          resource_kind: "corridor",
          valid_from: iso(20),
          valid_until: iso(260),
          state: "committed",
          commitment_token: "synthetic-token-1",
          ...prov("committed"),
          authoritative: true,
        },
        // committed pad booking at the destination hub
        {
          reservation_id: "55555555-5555-5555-5555-000000000002",
          tenant_id: TENANT,
          mission_id: MISSION,
          resource_key: `pad:${HUB_SOUTH.facility_id}`,
          resource_kind: "pad",
          valid_from: iso(300),
          valid_until: iso(420),
          state: "committed",
          ...prov("committed"),
          authoritative: true,
        },
        // a second mission's committed corridor (contention, visible on the map)
        {
          reservation_id: "55555555-5555-5555-5555-000000000003",
          tenant_id: TENANT,
          mission_id: MISSION_B,
          resource_key: `corridor:${EDGE_B.corridor_id}:segment-1`,
          resource_kind: "corridor",
          valid_from: iso(60),
          valid_until: iso(360),
          state: "tentative",
          ...prov("planned"),
        },
        // the protected volume that stays held while telemetry is uncertain
        {
          reservation_id: "55555555-5555-5555-5555-000000000004",
          tenant_id: TENANT,
          mission_id: MISSION,
          resource_key: `volume_cell:${EDGE_A.corridor_id}@cell-7`,
          resource_kind: "volume_cell",
          valid_from: iso(180),
          valid_until: iso(600),
          state: "unresolved",
          ...prov("committed"),
          authoritative: true,
        },
        {
          reservation_id: "55555555-5555-5555-5555-000000000005",
          tenant_id: TENANT,
          mission_id: MISSION_B,
          resource_key: `charger_bay:${HUB_NORTH.facility_id}:bay-2`,
          resource_kind: "charger_bay",
          valid_from: iso(600),
          valid_until: iso(2400),
          state: "completed",
          ...prov("committed"),
          authoritative: true,
        },
      ],
      ...prov("committed"),
    },
    listMissionReservations: {
      items: [],
      ...prov("committed"),
    },
    listMissionObservations: {
      items: observations(),
      ...prov("observed"),
    },
    getMissionUncertaintyState: {
      aircraft_id: AIRCRAFT_A,
      tenant_id: TENANT,
      state: "uncertain",
      last_accepted_measurement_time: iso(100),
      staleness_s: 42,
      protected_volume_retained: true,
      ...prov("observed"),
    },
    getMissionDecisionTrace: {
      mission_id: MISSION,
      tenant_id: TENANT,
      record: {
        config_release_sha:
          "sha256:0000000000000000000000000000000000000000000000000000000000000000",
        reason_codes: ["energy_reserve_shortfall", "pad_contention"],
        created_at: iso(5),
        input_hash: WORLD.provenance.bundle_manifest_sha256,
        deciding_actor: "actor-scheduler-a",
        outcome: "committed",
        committed: true,
      },
      steps: [
        { stage: "validate_identity", outcome: "pass", step: 1, detail: "duplicate check clean" },
        {
          stage: "freeze_world_snapshot",
          outcome: "pass",
          step: 2,
          detail: WORLD.provenance.snapshot_digest,
        },
        {
          stage: "rank_eligible_aircraft",
          outcome: "pass",
          step: 3,
          detail: "3 eligible, 1 rejected",
        },
        {
          stage: "generate_candidates",
          outcome: "pass",
          step: 4,
          detail: "22 corridor edges considered",
        },
        {
          stage: "reject_restricted",
          outcome: "rejected",
          step: 5,
          detail: "lane excluded: modelled keep-out (not official airspace data)",
        },
        {
          stage: "energy_and_recovery",
          outcome: "pass",
          step: 6,
          energy_margin_wh: 34,
          timing_uncertainty_s: 12,
        },
        { stage: "acquire_locks", outcome: "pass", step: 7, detail: "canonical resource order" },
        { stage: "recheck_committed", outcome: "pass", step: 8 },
        { stage: "insert_records", outcome: "pass", step: 9 },
      ],
      inputs: {
        snapshot_digest: WORLD.provenance.snapshot_digest,
        policy_version: WORLD.provenance.policy_version,
        config_release_sha:
          "sha256:0000000000000000000000000000000000000000000000000000000000000000",
        origin_facility_id: HUB_NORTH.facility_id,
        destination_facility_id: HUB_SOUTH.facility_id,
        priority_class: "standard",
        earliest_departure: iso(0),
        requested_deadline: iso(900),
        reservation_version: 4,
        restricted_keys: [`corridor:${EDGE_B.corridor_id}`],
        world_digest_at_decision: WORLD.provenance.boundary_hash,
      },
      eligible: [
        {
          rank: 1,
          aircraft_id: AIRCRAFT_A,
          status: "available",
          link_quality: "good",
          energy_margin_wh: 34,
          recovery_margin_wh: 18,
          lateness_us: 0,
          disturbance_units: 3,
          fairness_units: 1,
          total_units: 7,
        },
        {
          rank: 2,
          aircraft_id: AIRCRAFT_B,
          status: "charging",
          link_quality: "good",
          energy_margin_wh: 12,
          recovery_margin_wh: 6,
          lateness_us: 90_000_000,
          disturbance_units: 1,
          fairness_units: 4,
          total_units: 9,
        },
      ],
      ranking: [
        {
          aircraft_id: AIRCRAFT_A,
          candidate_id: "cand-a-1",
          strategy: "shortest-energy",
          rank: 1,
          total_units: 7,
          lateness_us: 0,
          energy_margin_wh: 34,
        },
        {
          aircraft_id: AIRCRAFT_B,
          candidate_id: "cand-b-1",
          strategy: "fewest-segments",
          rank: 2,
          total_units: 9,
          lateness_us: 90_000_000,
          energy_margin_wh: 12,
        },
      ],
      rejected: [
        {
          aircraft_id: AIRCRAFT_C,
          candidate_id: "cand-c-1",
          resource_key: `corridor:${EDGE_A.corridor_id}`,
          reason_code: "energy_reserve_shortfall",
          human_reason: "Aircraft 003 cannot reach the destination and keep its reserve.",
          technical_detail:
            "margin 4 Wh < required 15 Wh at 6 m/s headwind, config chandigarh-synthetic-p1",
          step: 6,
          step_name: "energy_and_recovery",
        },
        {
          candidate_id: "cand-geo-1",
          resource_key: `corridor:${EDGE_B.corridor_id}`,
          reason_code: "restricted airspace",
          human_reason: "This lane passes the modelled airfield keep-out, so it was refused.",
          technical_detail:
            "restriction modelled from a fixed 1500 m radius, not a published or surveyed airspace volume",
          step: 5,
          step_name: "reject_restricted",
        },
      ],
      selected_plan_version_id: "44444444-4444-4444-4444-000000000001",
      primary_reason_code: "energy_reserve_shortfall",
      primary_human_reason:
        "Aircraft 003 was refused: it cannot keep its energy reserve into a 6 m/s headwind.",
      lock_order: [`corridor:${EDGE_A.corridor_id}`, `pad:${HUB_SOUTH.facility_id}`],
      explanation_readable_in_s: 45,
      ...prov("committed"),
      authoritative: true,
    },
    listMissionPlanVersions: {
      items: [
        {
          plan_version_id: "44444444-4444-4444-4444-000000000001",
          mission_id: MISSION,
          route_hash: "9f8e7d6c5b4a39281706f5e4d3c2b1a09876543210fedcba9876543210fedcba",
          energy_margin_wh: 34,
          config_release_sha:
            "sha256:0000000000000000000000000000000000000000000000000000000000000000",
          created_at: iso(5),
          aircraft_id: AIRCRAFT_A,
          strategy: "shortest-energy",
          rank: 1,
          resource_keys: [`corridor:${EDGE_A.corridor_id}`, `pad:${HUB_SOUTH.facility_id}`],
          snapshot_digest: WORLD.provenance.snapshot_digest,
          ...prov("committed", true),
        },
        {
          plan_version_id: "44444444-4444-4444-4444-000000000002",
          mission_id: MISSION,
          route_hash: "1111111111111111111111111111111111111111111111111111111111111111",
          energy_margin_wh: 21,
          config_release_sha:
            "sha256:0000000000000000000000000000000000000000000000000000000000000000",
          created_at: iso(4),
          aircraft_id: AIRCRAFT_B,
          strategy: "fewest-segments",
          rank: 2,
          resource_keys: [`corridor:${EDGE_B.corridor_id}`],
          snapshot_digest: WORLD.provenance.snapshot_digest,
          ...prov("planned"),
        },
      ],
      ...prov("planned"),
    },
    listOrders: {
      items: [
        {
          order_id: "11111111-1111-1111-1111-111111111111",
          tenant_id: TENANT,
          mission_id: MISSION,
          status: "accepted",
          external_ref: "FIX-ORDER-001",
          priority_class: "standard",
          ...prov("synthetic"),
        },
        {
          order_id: "11111111-1111-1111-1111-111111111112",
          tenant_id: TENANT,
          mission_id: MISSION_B,
          status: "waiting",
          external_ref: "FIX-ORDER-002",
          priority_class: "priority",
          ...prov("synthetic"),
        },
      ],
      ...prov("synthetic"),
    },
    getOrder: {
      order_id: "11111111-1111-1111-1111-111111111111",
      tenant_id: TENANT,
      mission_id: MISSION,
      status: "accepted",
      external_ref: "FIX-ORDER-001",
      priority_class: "standard",
      ...prov("synthetic"),
    },
    listAuditEvents: {
      items: [
        {
          audit_id: "aaaaaaaa-0000-0000-0000-000000000001",
          tenant_id: TENANT,
          occurred_at: iso(5),
          actor_type: "service",
          actor_id: "actor-scheduler-a",
          operation_kind: "reserveMissionResources",
          resource_type: "corridor",
          resource_id: `corridor:${EDGE_A.corridor_id}`,
          request_id: "req-synthetic-1",
          reason_code: "committed",
          config_release_sha:
            "sha256:0000000000000000000000000000000000000000000000000000000000000000",
          prev_hash: "sha256:0000",
          record_hash: "sha256:1111",
          ...prov("committed", true),
        },
        {
          audit_id: "aaaaaaaa-0000-0000-0000-000000000002",
          tenant_id: TENANT,
          occurred_at: iso(9),
          actor_type: "service",
          actor_id: "actor-scheduler-a",
          operation_kind: "reserveMissionResources",
          resource_type: "aircraft",
          resource_id: AIRCRAFT_C,
          request_id: "req-synthetic-2",
          reason_code: "energy_reserve_shortfall",
          config_release_sha:
            "sha256:0000000000000000000000000000000000000000000000000000000000000000",
          prev_hash: "sha256:1111",
          record_hash: "sha256:2222",
          ...prov("committed", true),
        },
      ],
      ...prov("committed", true),
    },
    getSimulationRun: {
      run_identity: {
        bundle_sha256: WORLD.provenance.bundle_sha256,
        engine_version: "sim-1.0.0",
        scenario_sha256: WORLD.provenance.bundle_files["corridors.yaml"] ?? "",
        policy_version: WORLD.provenance.policy_version,
        config_release_sha256: WORLD.provenance.config_sha256,
        seed: WORLD.provenance.seed,
        python_version: "3.13",
        machine_architecture: "x86_64",
      },
      submitted_orders: 2400,
      accepted: 1920,
      rejected: 480,
      on_time_rate: 0.91,
      run_id: "run-20260930-synthetic",
      snapshot_digest: WORLD.provenance.snapshot_digest,
      event_hash: "sha256:synthetic-event-chain",
      frozen_at: WORLD.provenance.dataset_retrieved_at,
      ...prov("synthetic", false),
    },
    getSimulationReplayDiff: {
      run_a: runIdentity("sim-1.0.0"),
      run_b: runIdentity("sim-1.0.0"),
      event_hash_equal: true,
      metrics_equal: true,
      differing_metrics: [],
      replayed_against: "frozen-snapshot",
      current_world_digest: WORLD.provenance.boundary_hash,
      ...prov("synthetic", false),
    },
    compareScenarios: {
      run_a: runIdentity("sim-1.0.0"),
      run_b: runIdentity("sim-1.1.0"),
      verdict:
        "synthetic comparison: policy A serves more orders, policy B rejects fewer for energy. Not a recommendation.",
      compared_metrics: {
        on_time_rate: "0.91",
        rejected: "480",
        energy_per_completion_wh: "41.6",
      },
      scenario_id: WORLD.provenance.scenario_id,
      against: "contention-avoid-v2",
      ...prov("synthetic", false),
    },
  };
}

/**
 * The transport itself. `label` is user-visible text in the provenance strip, so it is
 * written to be read by a person deciding whether they can trust what they are looking at.
 */
export function createSyntheticTransport(): Transport {
  if (import.meta.env.PROD) {
    throw new Error("the synthetic dev transport must never be constructed in a production build");
  }
  const table = fixtures();
  const mode: TransportMode = "synthetic";
  const label = "synthetic dev transport (fixtures, not live data)";

  return {
    mode,
    label,
    async get<Id extends ReadOperationId>(
      id: Id,
      args: CallArgs = {},
    ): Promise<ApiResult<ResponseFor<Id>>> {
      await new Promise((resolve) => setTimeout(resolve, 40));
      const data = table[id] as ResponseFor<Id>;
      const meta: ResponseMeta = {
        transportMode: mode,
        transportLabel: label,
        operation: id,
        status: 200,
        receivedAt: new Date().toISOString(),
        provenance: { ...PROVENANCE, source_kind: "synthetic" },
      };
      void args;
      return { ok: true, data, meta };
    },
  };
}

/** Same fixture shape, but reporting as unreachable: how a dead API must look. */
export function unavailableResult(id: ReadOperationId): ApiResult<never> {
  return {
    ok: false,
    error: {
      kind: "network",
      message: "API unreachable - start it with `make api`",
      operation: id,
    },
  };
}
