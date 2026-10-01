/**
 * The semantic palette: one entry per data state, each with a SECOND channel.
 *
 * Colours are read from `src/styles/tokens.css` at runtime so the token file stays the
 * single source (and so `npm run audit:contrast` measures the same values the map draws).
 * The rule this module enforces: hue never carries meaning on its own. Every state also
 * declares a dash pattern, a marker glyph, an opacity and a text tag, so a
 * deuteranopic / protanopic / tritanopic viewer - or a projector, or a printed handout -
 * still separates planned from committed from observed.
 *
 * Simulation only. Not authorised for flight.
 */

import type { DataState } from "./api/types";

export interface StateStyle {
  readonly state: DataState;
  readonly label: string;
  readonly token: string;
  /** Non-colour channel 1: SVG/canvas dash pattern. */
  readonly dash: readonly number[];
  readonly dashCss: string;
  /** Non-colour channel 2: marker glyph drawn on the map and in the legend. */
  readonly glyph: string;
  /** Non-colour channel 3: fill opacity. */
  readonly fillOpacity: number;
  /** Non-colour channel 4: the tag printed next to every instance. */
  readonly tag: string;
  readonly meaning: string;
  readonly notThis: string;
}

export const STATE_STYLES: Record<DataState, StateStyle> = {
  planned: {
    state: "planned",
    label: "Planned",
    token: "--state-planned",
    dash: [6, 4],
    dashCss: "6 4",
    glyph: "◌",
    fillOpacity: 0.28,
    tag: "PLAN",
    meaning: "A planner proposal. It reserves nothing.",
    notThis: "Not a commitment and not a measurement.",
  },
  committed: {
    state: "committed",
    label: "Committed",
    token: "--state-committed",
    dash: [],
    dashCss: "none",
    glyph: "■",
    fillOpacity: 0.55,
    tag: "LEDGER",
    meaning: "Written to the reservation ledger in a database transaction.",
    notThis: "Still not an aircraft command.",
  },
  observed: {
    state: "observed",
    label: "Observed",
    token: "--state-observed",
    dash: [1, 3],
    dashCss: "1 3",
    glyph: "▲",
    fillOpacity: 0.9,
    tag: "MEASURED",
    meaning: "A telemetry measurement that arrived in sequence and is not stale.",
    notThis: "Not a prediction and not a clearance statement.",
  },
  synthetic: {
    state: "synthetic",
    label: "Synthetic",
    token: "--state-synthetic",
    dash: [12, 3, 2, 3],
    dashCss: "12 3 2 3",
    glyph: "◆",
    fillOpacity: 0.35,
    tag: "SYNTH",
    meaning: "Generated scenario input: a fixture, a modelled corridor or a dev transport.",
    notThis: "Never a measurement, never an approval, never a surveyed geometry.",
  },
  uncertain: {
    state: "uncertain",
    label: "Uncertain",
    token: "--state-uncertain",
    dash: [2, 2],
    dashCss: "2 2",
    glyph: "◎",
    fillOpacity: 0.18,
    tag: "± UNKNOWN",
    meaning: "Telemetry is stale, lost or unreconciled. The last protected volume stays held.",
    notThis: "Uncertain is NOT clear airspace. It is never drawn as a smooth transition.",
  },
  rejected: {
    state: "rejected",
    label: "Rejected",
    token: "--state-rejected",
    dash: [3, 1],
    dashCss: "3 1",
    glyph: "✕",
    fillOpacity: 0.75,
    tag: "REJECTED",
    meaning: "A candidate the commit transaction refused, with a reason on record.",
    notThis: "Not a failure to display: the reason is the deliverable.",
  },
};

export const STATE_ORDER: readonly DataState[] = [
  "committed",
  "planned",
  "observed",
  "uncertain",
  "rejected",
  "synthetic",
];

/** CSS custom properties that are not state colours but are needed on the canvas. */
export const CANVAS_TOKENS = [
  "--map-basemap",
  "--map-land",
  "--map-boundary",
  "--surface-0",
  "--surface-1",
  "--text-primary",
  "--text-secondary",
  "--focus",
] as const;

let cache = new Map<string, string>();

/** Resolve one custom property from :root. Falls back loudly rather than silently. */
export function token(name: string): string {
  const hit = cache.get(name);
  if (hit !== undefined) {
    return hit;
  }
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  if (value === "") {
    throw new Error(`design token ${name} is not defined - styles/tokens.css must load first`);
  }
  cache.set(name, value);
  return value;
}

export function invalidateTokenCache(): void {
  cache = new Map();
}

export function stateColor(state: DataState, alpha = 1): string {
  const [r, g, b] = hexToRgb(token(STATE_STYLES[state].token));
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

export function stateRgb(state: DataState): [number, number, number] {
  return hexToRgb(token(STATE_STYLES[state].token));
}

export function hexToRgb(hex: string): [number, number, number] {
  const value = hex.replace("#", "");
  const full =
    value.length === 3
      ? value
          .split("")
          .map((c) => c + c)
          .join("")
      : value;
  const n = Number.parseInt(full.slice(0, 6), 16);
  if (Number.isNaN(n)) {
    throw new Error(`not a hex colour: ${hex}`);
  }
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

export function styleFor(state: DataState): StateStyle {
  return STATE_STYLES[state];
}
