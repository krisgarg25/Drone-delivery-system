/**
 * The frozen synthetic world model the map draws, and the only place that knows how to
 * turn contract data into geometry.
 *
 * Hard constraint, stated once so nobody re-derives it: `contracts/openapi/v1.yaml`
 * carries positions for exactly one thing - telemetry observations
 * (`TelemetryObservationResponse.position`). Facilities, plans and reservations carry
 * identifiers and `resource_key`s (`corridor:<id>`, `pad:<id>`, pattern-validated in the
 * contract) but no coordinates. So the basemap, corridor centrelines and facility pads
 * come from the frozen scenario bundle under `data/processed/`, copied by
 * `npm run build:synthetic-data` into `data/synthetic-world.json`, and ledger rows join
 * onto it through `resource_key`. Anything that cannot be joined is drawn as a rejected
 * marker with its reason, never as an invented line.
 *
 * Everything here is synthetic: `authoritative: false`, `synthetic: true`,
 * `requires_survey: true`, and the corridor label is the one the GIS config uses -
 * "Synthetic corridor model (planning heuristic only, not a clearance assessment)".
 *
 * Simulation only. Not authorised for flight.
 */

import raw from "./data/synthetic-world.json";

export interface LngLat {
  0: number;
  1: number;
}

export interface WorldProvenance {
  scenario_id: string;
  title: string;
  seed: number;
  policy_version: string;
  horizon_minutes: number;
  topology: { hubs: number; nodes: number };
  synthetic_flags: Record<string, boolean>;
  bundle_files: Record<string, string>;
  bundle_sha256: string;
  bundle_manifest_sha256: string;
  snapshot_digest: string;
  dataset_id: string;
  dataset_retrieved_at: string;
  dataset_produced_at: string;
  config_version: string;
  config_sha256: string;
  boundary_source: string;
  boundary_relation_id: number;
  boundary_hash: string;
  boundary_extract_date: string;
  licence: string;
  attribution: string;
  weather: {
    station: string;
    source: string;
    retrieved_at: string;
    expires_at: string;
    freshness: string;
  };
  corridor_label: string;
  corridor_summary: {
    note: string;
    basis: string;
    method: string;
    planning_heuristic_only: boolean;
    requires_survey: boolean;
    safety_claim: boolean;
    corridors: number;
    total_length_m: number;
    what_this_does_not_prove: string[];
  };
  service_bbox: [number, number, number, number];
  what_this_does_not_prove: string;
}

export interface PadFeature {
  facility_id: string;
  name: string;
  kind: string;
  position: [number, number];
  pads: number;
  charger_bays: number;
  band_m_agl: number;
  altitude_reference: string;
  synthetic: boolean;
  authoritative: boolean;
}

export interface CorridorEdge {
  corridor_id: string;
  from: string;
  to: string;
  band: string;
  width_m: number;
  length_m: number;
  max_wind_ms: number;
  altitude_m: number;
  altitude_reference: string;
  line: [number, number][];
  synthetic: boolean;
  authoritative: boolean;
}

export interface CandidateCorridor {
  corridor_id: string;
  length_m: number;
  basis: string;
  lines: [number, number][][];
}

export interface RestrictionBuffer {
  restriction_id: string;
  label: string;
  official: boolean;
  authoritative: boolean;
  synthetic: boolean;
  basis: string;
  polygon: [number, number][][];
}

export interface SyntheticWorld {
  notice: string;
  authoritative: boolean;
  provenance: WorldProvenance;
  layers: {
    basemap: { id: string; label: string; bbox: [number, number, number, number] };
    corridor_centerline: { id: string; edges: CorridorEdge[] };
    candidate_corridors: { id: string; lines: CandidateCorridor[] };
    facility_pads: { id: string; facilities: PadFeature[] };
    restriction_buffers: { id: string; polygons: RestrictionBuffer[] };
    weather: { id: string; trace: { minute: number; wind_ms: number; headwind_bp: number }[] };
  };
}

export const WORLD = raw as unknown as SyntheticWorld;

if (WORLD.authoritative || WORLD.notice !== "Simulation only. Not authorised for flight.") {
  throw new Error("the world asset must be non-authoritative and carry the standing statement");
}

export const CORRIDOR_LABEL = WORLD.provenance.corridor_label;
export const SERVICE_BBOX = WORLD.layers.basemap.bbox;

export function corridors(): CorridorEdge[] {
  return WORLD.layers.corridor_centerline.edges;
}

export function corridorById(id: string): CorridorEdge | undefined {
  return corridors().find((edge) => edge.corridor_id === id);
}

export function pads(): PadFeature[] {
  return WORLD.layers.facility_pads.facilities;
}

export function padById(id: string): PadFeature | undefined {
  return pads().find((pad) => pad.facility_id === id);
}

export function restrictions(): RestrictionBuffer[] {
  return WORLD.layers.restriction_buffers.polygons;
}

export function candidateCorridors(): CandidateCorridor[] {
  return WORLD.layers.candidate_corridors.lines;
}

export function windAt(minute: number): { wind_ms: number; headwind_bp: number } {
  const trace = WORLD.layers.weather.trace;
  const clamped = Math.max(0, Math.min(trace.length - 1, Math.round(minute)));
  return trace[clamped];
}

/**
 * `corridor:<id>` / `pad:<id>` per the contract's resource-key pattern. The ledger's real
 * keys carry extra path segments (`corridor:hub-north:node-a:segment-7`), so the lookup
 * matches the longest known corridor id the key starts with instead of requiring equality.
 */
export function corridorFor(remainder: string): CorridorEdge | undefined {
  const known = corridors()
    .filter(
      (edge) => remainder === edge.corridor_id || remainder.startsWith(`${edge.corridor_id}:`),
    )
    .sort((a, b) => b.corridor_id.length - a.corridor_id.length);
  return known[0];
}

export function padFor(remainder: string): PadFeature | undefined {
  return pads().find(
    (pad) => remainder === pad.facility_id || remainder.startsWith(`${pad.facility_id}:`),
  );
}

/** Along-track interpolation used for plan and reservation geometry. */
export function pointAlong(edge: CorridorEdge, t: number): [number, number] {
  const clamped = Math.max(0, Math.min(1, t));
  const segments = edge.line.length - 1;
  if (segments <= 0) {
    return edge.line[0] ?? [0, 0];
  }
  const scaled = clamped * segments;
  const index = Math.min(segments - 1, Math.floor(scaled));
  const fraction = scaled - index;
  const [lon1, lat1] = edge.line[index];
  const [lon2, lat2] = edge.line[index + 1];
  return [lon1 + (lon2 - lon1) * fraction, lat1 + (lat2 - lat1) * fraction];
}

/** A sub-line of a corridor between two along-track fractions. */
export function sliceAlong(edge: CorridorEdge, from: number, to: number): [number, number][] {
  const steps = Math.max(2, Math.round(Math.abs(to - from) * 24));
  const out: [number, number][] = [];
  for (let i = 0; i <= steps; i += 1) {
    out.push(pointAlong(edge, from + ((to - from) * i) / steps));
  }
  return out;
}

/** Great-circle-ish metre offset good enough for a display ring, never a clearance. */
export function ringAround(
  center: [number, number],
  radiusM: number,
  steps = 48,
): [number, number][] {
  const dLat = radiusM / 111_320;
  const dLon = radiusM / (111_320 * Math.max(0.2, Math.cos((center[1] * Math.PI) / 180)));
  const ring: [number, number][] = [];
  for (let i = 0; i <= steps; i += 1) {
    const angle = (i / steps) * Math.PI * 2;
    ring.push([center[0] + dLon * Math.cos(angle), center[1] + dLat * Math.sin(angle)]);
  }
  return ring;
}
