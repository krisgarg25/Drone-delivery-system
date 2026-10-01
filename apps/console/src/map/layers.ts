/**
 * The nine-layer model from technical-build/11-simulation-routing-engine.md section 12,
 * plus the rule that each layer's meaning is carried by a SECOND channel:
 *
 *   basemap              no data state
 *   corridor centerline  synthetic  (dash-dot, diamond)
 *   occupied reservation committed  (solid, filled square) / tentative (planned channel)
 *   proposed plan        planned    (dashed, hollow circle)
 *   observed telemetry   observed   (dotted, triangle)
 *   uncertainty volume   uncertain  (hatched ring, double circle, low fill opacity)
 *   facility pads        synthetic  + a status tag per pad
 *   rejected requests    rejected   (cross, X marker, reason text)
 *   weather overlay      synthetic  (magnitude arrows, direction not in the dataset)
 *
 * The tenth layer is the modelled keep-out ring. It exists to prove the console refuses
 * to present an invented airspace volume as official: its label says so, its tooltip says
 * so, and it is drawn in the synthetic channel.
 *
 * Simulation only. Not authorised for flight.
 */

import { STATE_STYLES } from "../lib/palette";
import type { DataState, Schemas } from "../lib/api/types";
import {
  WORLD,
  corridorFor,
  padFor,
  pointAlong,
  restrictions,
  ringAround,
  sliceAlong,
} from "./world";

export type LayerId =
  | "basemap"
  | "corridor-centerline"
  | "occupied-reservation"
  | "proposed-plan"
  | "observed-telemetry"
  | "uncertainty-volume"
  | "facility-pads"
  | "rejected-request"
  | "weather-overlay"
  | "modelled-restriction";

export type LineKind = "path" | "arrow";

export interface MapLine {
  kind: LineKind;
  coordinates: [number, number][];
  widthPx: number;
  state: DataState;
  layer: LayerId;
  title: string;
  detail: string;
  /** The non-colour channel actually drawn, not just declared. */
  dash: readonly number[];
}

export interface MapPoint {
  coordinates: [number, number];
  state: DataState;
  layer: LayerId;
  glyph: string;
  radiusPx: number;
  title: string;
  detail: string;
  /** Optional text drawn beside the glyph: every state also has a word. */
  label?: string;
}

export interface MapArea {
  polygon: [number, number][];
  state: DataState;
  layer: LayerId;
  elevationM: number;
  opacity: number;
  dash: readonly number[];
  title: string;
  detail: string;
}

export interface LayerDefinition {
  readonly id: LayerId;
  readonly order: number;
  readonly label: string;
  readonly states: readonly DataState[];
  readonly source: string;
  readonly channel: string;
  readonly warning: string;
}

export const LAYER_DEFS: readonly LayerDefinition[] = [
  {
    id: "basemap",
    order: 1,
    label: "Basemap",
    states: [],
    source: "Frozen service bbox and grid. No tile server, no live imagery, no street data.",
    channel: "none: it carries no data state",
    warning: "Display geometry only. It is not a chart and not a survey.",
  },
  {
    id: "corridor-centerline",
    order: 2,
    label: "Corridor centreline",
    states: ["synthetic"],
    source: "synthetic-world.json: 22 scenario edges plus 60 OSM-aligned candidate lines.",
    channel: `dash ${STATE_STYLES.synthetic.dashCss}, diamond ${STATE_STYLES.synthetic.glyph}, SYNTH tag`,
    warning: WORLD.provenance.corridor_summary.note,
  },
  {
    id: "occupied-reservation",
    order: 3,
    label: "Occupied reservation",
    states: ["committed", "planned"],
    source: "GET /v1/reservations - ledger rows joined to corridors by resource_key.",
    channel: "committed: solid line, filled square, LEDGER tag; tentative: planned channel",
    warning: "A committed reservation is a database record, not a clearance and not a command.",
  },
  {
    id: "proposed-plan",
    order: 4,
    label: "Proposed plan",
    states: ["planned"],
    source: "GET /v1/missions/{id}/plan-versions - candidate route with its energy margin.",
    channel: "dashed line, hollow circle, PLAN tag, 28% fill",
    warning: "A proposal reserves nothing. It must never share a style with a commitment.",
  },
  {
    id: "observed-telemetry",
    order: 5,
    label: "Observed telemetry",
    states: ["observed"],
    source:
      "GET /v1/missions/{id}/observations - positions from the contract's only geometry field.",
    channel: "dotted track, triangle ▲, MEASURED tag; out-of-order frames flagged separately",
    warning: "Observed is a measurement, not truth: a late frame never overwrites a newer one.",
  },
  {
    id: "uncertainty-volume",
    order: 6,
    label: "Uncertainty volume",
    states: ["uncertain"],
    source: "GET /v1/missions/{id}/uncertainty - staleness plus the retained protected volume.",
    channel: "hatched 2 2 ring, double circle ◎, ± UNKNOWN tag, 18% fill, extruded elevation",
    warning: "Lost telemetry means uncertainty, never clear airspace. The volume stays held.",
  },
  {
    id: "facility-pads",
    order: 7,
    label: "Facility pads",
    states: ["synthetic", "committed"],
    source: "synthetic-world.json pads plus GET /v1/facilities status and capacity.",
    channel: "diamond ◆ for the model, square ■ for a committed pad booking, text label",
    warning: "Every pad name here is fictional. No candidate node is a real hospital or business.",
  },
  {
    id: "rejected-request",
    order: 8,
    label: "Rejected request markers",
    states: ["rejected"],
    source: "GET /v1/missions/{id}/decision - rejected candidates with reason and evidence.",
    channel: "cross ✕, strikethrough ring, REJECTED tag, human reason as map text",
    warning: "A rejection is a first-class result. Hiding it would be the misleading demo.",
  },
  {
    id: "weather-overlay",
    order: 9,
    label: "Weather overlay",
    states: ["synthetic"],
    source: "synthetic-world.json frozen wind trace (station IXC-synthetic), replayed not fetched.",
    channel: "arrow shaft for magnitude along the corridor axis, number label per edge",
    warning:
      "The trace has no wind direction. Direction is deliberately not invented; arrows show modelled headwind magnitude only.",
  },
  {
    id: "modelled-restriction",
    order: 10,
    label: "Modelled keep-out (not official)",
    states: ["synthetic"],
    source: "A fixed 1500 m ring drawn around a fictional airfield pad as a demo input.",
    channel: "dash-dot ring, diamond ◆, SYNTH tag, and the word NOT OFFICIAL in the label",
    warning:
      "This is not published, surveyed or approved airspace. It is a modelled input for one demonstration.",
  },
] as const;

export const LAYER_IDS: readonly LayerId[] = LAYER_DEFS.map((def) => def.id);

/* ------------------------------------------------------------------ dataset in */

export interface MapDataset {
  facilities: Schemas["FacilityResponseList"]["items"];
  reservations: Schemas["ReservationListResponse"]["items"];
  plans: Schemas["PlanVersionResponseList"]["items"];
  observations: Schemas["TelemetryListResponse"]["items"];
  uncertainty: Schemas["UncertaintyStateResponse"] | null;
  decision: Schemas["DecisionTraceResponse"] | null;
  /** The transport mode is part of the geometry: a synthetic reservation cannot be drawn as a commitment. */
  syntheticTransport: boolean;
}

export interface BuiltLayers {
  lines: MapLine[];
  points: MapPoint[];
  areas: MapArea[];
  counts: Record<LayerId, number>;
}

const EMPTY_COUNTS: Record<LayerId, number> = {
  basemap: 0,
  "corridor-centerline": 0,
  "occupied-reservation": 0,
  "proposed-plan": 0,
  "observed-telemetry": 0,
  "uncertainty-volume": 0,
  "facility-pads": 0,
  "rejected-request": 0,
  "weather-overlay": 0,
  "modelled-restriction": 0,
};

export function stateDash(state: DataState): readonly number[] {
  return STATE_STYLES[state].dash;
}

function tag(state: DataState): string {
  return STATE_STYLES[state].tag;
}

/** Corridor centrelines: the scenario edges, plus the fainter OSM-aligned candidates. */
function centerlines(lines: MapLine[]): void {
  for (const edge of WORLD.layers.corridor_centerline.edges) {
    lines.push({
      kind: "path",
      coordinates: edge.line,
      widthPx: 2.5,
      state: "synthetic",
      layer: "corridor-centerline",
      title: `${edge.corridor_id} (band ${edge.band})`,
      detail: [
        `${edge.from} -> ${edge.to}`,
        `${edge.length_m} m, ${edge.width_m} m wide, ${edge.altitude_m} m ${edge.altitude_reference}`,
        `max wind ${edge.max_wind_ms} m/s`,
        `${tag("synthetic")} - ${WORLD.provenance.corridor_label}`,
        WORLD.provenance.corridor_summary.note,
      ].join("\n"),
      dash: stateDash("synthetic"),
    });
  }
  for (const candidate of WORLD.layers.candidate_corridors.lines) {
    for (const line of candidate.lines.slice(0, 3)) {
      lines.push({
        kind: "path",
        coordinates: line,
        widthPx: 1,
        state: "synthetic",
        layer: "corridor-centerline",
        title: `candidate ${candidate.corridor_id}`,
        detail: `OSM-aligned candidate, ${candidate.length_m} m. ${WORLD.provenance.corridor_label}. Basis: ${candidate.basis}.`,
        dash: [1, 4],
      });
    }
  }
}

function reservations(data: MapDataset, lines: MapLine[], points: MapPoint[]): void {
  for (const row of data.reservations) {
    const [kind, ...rest] = row.resource_key.split(":");
    const remainder = rest.join(":");
    const state: DataState = row.state === "tentative" ? "planned" : "committed";
    const when = `${row.valid_from.slice(11, 19)}-${row.valid_until.slice(11, 19)}Z`;
    const title = `${kind} reservation ${tag(state)}`;
    const detail = [
      `resource_key ${row.resource_key}`,
      `state ${row.state}, window ${when}`,
      `source_kind ${row.source_kind}, authoritative ${row.authoritative}`,
      data.syntheticTransport
        ? "DEV FIXTURE: the ledger was not contacted."
        : row.simulation_notice,
    ].join("\n");

    if (kind === "corridor" || kind === "crossing" || kind === "volume_cell") {
      const edge = corridorFor(remainder);
      if (edge) {
        lines.push({
          kind: "path",
          coordinates: sliceAlong(edge, 0.08, 0.92),
          widthPx: kind === "volume_cell" ? 14 : 6,
          state,
          layer: "occupied-reservation",
          title,
          detail,
          dash: stateDash(state),
        });
        continue;
      }
    }
    if (kind === "pad" || kind === "charger_bay") {
      const pad = padFor(remainder);
      if (pad) {
        points.push({
          coordinates: pad.position,
          state,
          layer: "occupied-reservation",
          glyph: STATE_STYLES[state].glyph,
          radiusPx: 11,
          title,
          detail: `${detail}\n${pad.name}: ${pad.pads} pads, ${pad.charger_bays} charger bays`,
          label: `${pad.name} ${when}`,
        });
        continue;
      }
    }
    // No geometry can be joined: say so instead of inventing a line.
    points.push({
      coordinates: [WORLD.layers.basemap.bbox[0] + 0.01, WORLD.layers.basemap.bbox[1] + 0.01],
      state,
      layer: "occupied-reservation",
      glyph: "?",
      radiusPx: 9,
      title,
      detail: `${detail}\nNo corridor or pad geometry matches this resource_key in the frozen bundle, so it is listed without a position rather than drawn as a guess.`,
    });
  }
}

function plans(data: MapDataset, lines: MapLine[]): void {
  for (const plan of data.plans) {
    for (const key of plan.resource_keys ?? []) {
      const [kind, ...rest] = key.split(":");
      if (kind !== "corridor") {
        continue;
      }
      const edge = corridorFor(rest.join(":"));
      if (!edge) {
        continue;
      }
      const selected = data.decision?.selected_plan_version_id === plan.plan_version_id;
      lines.push({
        kind: "path",
        coordinates: edge.line,
        widthPx: selected ? 4 : 2.5,
        state: selected ? "planned" : "planned",
        layer: "proposed-plan",
        title: `${plan.strategy ?? "plan"} rank ${plan.rank ?? "-"} ${tag("planned")}`,
        detail: [
          `plan_version ${plan.plan_version_id}`,
          `energy margin ${plan.energy_margin_wh} Wh, route_hash ${plan.route_hash.slice(0, 12)}…`,
          `snapshot ${plan.snapshot_digest?.slice(0, 18) ?? "not stated"}`,
          selected
            ? "This is the plan the trace selected - it is still a proposal until the ledger writes it."
            : "Not selected. Shown because a rejected candidate is evidence, not noise.",
          plan.simulation_notice,
        ].join("\n"),
        dash: stateDash("planned"),
      });
    }
  }
}

function telemetry(data: MapDataset, lines: MapLine[], points: MapPoint[]): void {
  const bySequence = [...data.observations].sort((a, b) => a.sequence - b.sequence);
  const track = bySequence.map((row) => [row.position.lon, row.position.lat] as [number, number]);
  if (track.length > 1) {
    lines.push({
      kind: "path",
      coordinates: track,
      widthPx: 3,
      state: "observed",
      layer: "observed-telemetry",
      title: `Observed track ${tag("observed")}`,
      detail: `${bySequence.length} frames, sequences ${bySequence[0].sequence}-${bySequence[bySequence.length - 1].sequence}. Positions are the only coordinates the API contract carries.`,
      dash: stateDash("observed"),
    });
  }
  bySequence.forEach((row, index) => {
    const last = index === bySequence.length - 1;
    if (!last && bySequence.length > 4 && index % 2 !== 0) {
      return;
    }
    points.push({
      coordinates: [row.position.lon, row.position.lat],
      state: "observed",
      layer: "observed-telemetry",
      glyph: STATE_STYLES.observed.glyph,
      radiusPx: last ? 12 : 7,
      title: last ? `Last observed position ${tag("observed")}` : `Frame ${row.sequence}`,
      detail: [
        `${row.position.altitude_m} m ${row.position.altitude_reference}`,
        `measured ${row.measurement_time}, received ${row.received_time}`,
        row.out_of_order
          ? "OUT OF ORDER: this late frame never overwrote a newer measurement."
          : "in sequence",
        `source ${row.source ?? "not stated"}, authoritative ${row.authoritative}`,
        data.syntheticTransport ? "DEV FIXTURE telemetry." : row.simulation_notice,
      ].join("\n"),
      ...(last ? { label: `seq ${row.sequence}` } : {}),
    });
  });
}

function uncertainty(
  data: MapDataset,
  areas: MapArea[],
  points: MapPoint[],
  lines: MapLine[],
): void {
  const state = data.uncertainty;
  if (!state || state.state === "clear") {
    return;
  }
  const last = [...data.observations].sort((a, b) => a.sequence - b.sequence).at(-1);
  const staleSeconds = state.staleness_s ?? 0;
  const radius = 120 + Math.min(900, staleSeconds * 8);
  const detail = [
    `state ${state.state}, staleness ${staleSeconds} s`,
    `last accepted measurement ${state.last_accepted_measurement_time ?? "not stated"}`,
    `protected volume retained: ${state.protected_volume_retained}`,
    "Lost or stale telemetry means uncertainty, never clear airspace: the last protected volume stays reserved until reconciled.",
    "Drawn as a static hatched ring. It never fades in - an animation would read as smoothing.",
    state.simulation_notice,
  ].join("\n");

  if (last) {
    const ring = ringAround([last.position.lon, last.position.lat], radius);
    areas.push({
      polygon: ring,
      state: "uncertain",
      layer: "uncertainty-volume",
      elevationM: 90,
      opacity: STATE_STYLES.uncertain.fillOpacity,
      dash: stateDash("uncertain"),
      title: `Uncertainty volume ${tag("uncertain")}`,
      detail: `${detail}\nRadius ${Math.round(radius)} m is staleness scaled for legibility, not a surveyed position error.`,
    });
    points.push({
      coordinates: [last.position.lon, last.position.lat],
      state: "uncertain",
      layer: "uncertainty-volume",
      glyph: STATE_STYLES.uncertain.glyph,
      radiusPx: 14,
      title: `Position uncertain: ${state.state} ${tag("uncertain")}`,
      detail,
      label: `± ${staleSeconds}s`,
    });
  }

  // The retained ledger volume for the same mission stays visible underneath.
  for (const row of data.reservations) {
    if (row.state !== "unresolved") {
      continue;
    }
    const [kind, ...rest] = row.resource_key.split(":");
    if (kind !== "corridor" && kind !== "volume_cell") {
      continue;
    }
    const edge = corridorFor(rest.join(":"));
    if (!edge) {
      continue;
    }
    lines.push({
      kind: "path",
      coordinates: edge.line,
      widthPx: 18,
      state: "uncertain",
      layer: "uncertainty-volume",
      title: `Retained protected volume ${tag("uncertain")}`,
      detail: `${edge.corridor_id} stays held while ${state.aircraft_id} is ${state.state}.`,
      dash: [2, 2],
    });
  }
}

function facilities(data: MapDataset, points: MapPoint[]): void {
  const statusById = new Map(data.facilities.map((row) => [row.facility_id, row]));
  for (const pad of WORLD.layers.facility_pads.facilities) {
    const row = statusById.get(pad.facility_id);
    const outage = row?.status === "outage" || row?.status === "closed";
    const state: DataState = outage ? "rejected" : "synthetic";
    points.push({
      coordinates: pad.position,
      state,
      layer: "facility-pads",
      glyph: outage ? STATE_STYLES.rejected.glyph : STATE_STYLES.synthetic.glyph,
      radiusPx: pad.kind === "hub" ? 12 : 9,
      title: `${pad.name} - ${pad.kind} ${outage ? "(outage)" : ""}`.trim(),
      detail: [
        `facility_id ${pad.facility_id}`,
        `${pad.pads} pads, ${pad.charger_bays} charger bays, ${pad.band_m_agl} m ${pad.altitude_reference}`,
        row
          ? `API status ${row.status}${row.outage_reason ? ` (${row.outage_reason})` : ""}, source_kind ${row.source_kind}`
          : "No API status row joined for this pad.",
        "Fictional facility. This product never labels a candidate node with a real hospital or business name.",
        !pad.authoritative ? "authoritative: false" : "unexpected authority flag",
      ].join("\n"),
      label: pad.name.replace(" (fictional)", ""),
    });
  }
}

function rejected(data: MapDataset, points: MapPoint[]): void {
  for (const row of data.decision?.rejected ?? []) {
    const [kind, ...rest] = (row.resource_key ?? "").split(":");
    const edge = kind === "corridor" ? corridorFor(rest.join(":")) : undefined;
    const pad = kind === "pad" || kind === "charger_bay" ? padFor(rest.join(":")) : undefined;
    const fallback = edge?.line[0] ??
      pad?.position ?? [WORLD.layers.basemap.bbox[0] + 0.02, WORLD.layers.basemap.bbox[1] + 0.02];
    points.push({
      coordinates: [fallback[0], fallback[1]],
      state: "rejected",
      layer: "rejected-request",
      glyph: STATE_STYLES.rejected.glyph,
      radiusPx: 13,
      title: `Rejected: ${row.reason_code}`,
      detail: [
        row.human_reason,
        row.technical_detail,
        `step ${row.step ?? "?"} ${row.step_name ?? ""}`.trim(),
        `candidate ${row.candidate_id ?? "n/a"}, aircraft ${row.aircraft_id ?? "n/a"}`,
        "Rejection reasons are stored records read from the trace endpoint, never recomputed in the browser.",
      ]
        .filter((line) => line !== "")
        .join("\n"),
      label: row.reason_code,
    });
  }
}

function weather(data: MapDataset, lines: MapLine[], points: MapPoint[]): void {
  const trace = WORLD.layers.weather.trace;
  const step = Math.max(1, Math.floor(trace.length / 6));
  const edges = WORLD.layers.corridor_centerline.edges;
  edges.forEach((edge, index) => {
    const sample = trace[(index * step) % trace.length];
    const from = pointAlong(edge, 0.35);
    const to = pointAlong(edge, 0.65);
    lines.push({
      kind: "arrow",
      coordinates: [from, to],
      widthPx: 2 + sample.wind_ms / 2,
      state: "synthetic",
      layer: "weather-overlay",
      title: `Modelled headwind ${sample.wind_ms} m/s`,
      detail: [
        `trace minute ${sample.minute} of ${trace.length}`,
        `headwind basis points ${sample.headwind_bp}`,
        `station ${WORLD.provenance.weather.station}, source ${WORLD.provenance.weather.source}`,
        `retrieved ${WORLD.provenance.weather.retrieved_at}, expires ${WORLD.provenance.weather.expires_at} (${WORLD.provenance.weather.freshness})`,
        "The frozen trace carries magnitude only. The arrow points along the corridor axis because no direction is in the data - it is not a wind bearing.",
        data.syntheticTransport ? "DEV FIXTURE transport." : WORLD.notice,
      ].join("\n"),
      dash: [10, 2],
    });
    points.push({
      coordinates: to,
      state: "synthetic",
      layer: "weather-overlay",
      glyph: `${sample.wind_ms}`,
      radiusPx: 0.01,
      title: `${sample.wind_ms} m/s`,
      detail: `Modelled wind magnitude at trace minute ${sample.minute}.`,
    });
  });
}

function modelledRestriction(areas: MapArea[]): void {
  for (const buffer of restrictions()) {
    for (const ring of buffer.polygon) {
      areas.push({
        polygon: ring,
        state: "synthetic",
        layer: "modelled-restriction",
        elevationM: 0,
        opacity: 0.08,
        dash: STATE_STYLES.synthetic.dash,
        title: buffer.label,
        detail: [
          `restriction_id ${buffer.restriction_id}`,
          buffer.basis,
          `official: ${buffer.official}, authoritative: ${buffer.authoritative}`,
          "An airport buffer in this product is a modelled input. It is never presented as published or approved airspace, and no clearance is implied.",
        ].join("\n"),
      });
    }
  }
}

export function buildLayers(data: MapDataset): BuiltLayers {
  const lines: MapLine[] = [];
  const points: MapPoint[] = [];
  const areas: MapArea[] = [];
  const counts = { ...EMPTY_COUNTS };

  centerlines(lines);
  reservations(data, lines, points);
  plans(data, lines);
  telemetry(data, lines, points);
  uncertainty(data, areas, points, lines);
  facilities(data, points);
  rejected(data, points);
  weather(data, lines, points);
  modelledRestriction(areas);

  for (const line of lines) {
    counts[line.layer] += 1;
  }
  for (const point of points) {
    counts[point.layer] += 1;
  }
  for (const area of areas) {
    counts[area.layer] += 1;
  }
  counts.basemap = 1;
  return { lines, points, areas, counts };
}

export const SYNTHETIC_CORRIDOR_LABEL = WORLD.provenance.corridor_label;
export const MAP_BASE_ATTRIBUTION = `Synthetic model. ${WORLD.provenance.attribution}. ${WORLD.notice}`;
