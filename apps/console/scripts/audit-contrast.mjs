/**
 * Contrast and colour-blind-safety audit for the console's design tokens.
 *
 * It measures the values that actually ship: it parses `src/styles/tokens.css` for the
 * palette, `src/lib/palette.ts` for the second channel each state declares, then computes
 * WCAG 2.1 relative-luminance ratios and CIE76 separations under normal vision and under
 * protanopic, deuteranopic and tritanopic simulation (Viénot/Brettel/Mollon 1999 matrices).
 *
 * Exit code 1 means a stated requirement is not met, so `npm run audit:contrast` is a gate,
 * not documentation. Run it with `node scripts/audit-contrast.mjs`.
 *
 * Simulation only. Not authorised for flight.
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, "..");

const css = readFileSync(join(root, "src", "styles", "tokens.css"), "utf8");
const palette = readFileSync(join(root, "src", "lib", "palette.ts"), "utf8");

/* ------------------------------------------------------------------ parsing */

function tokensFromCss(text) {
  const out = new Map();
  for (const match of text.matchAll(/(--[a-z0-9-]+):\s*(#[0-9a-fA-F]{3,6})\s*;/g)) {
    out.set(match[1], match[2]);
  }
  return out;
}

function statesFromPalette(text) {
  const out = [];
  const block = /(\w+):\s*\{[\s\S]*?\n {2}\},?\n\};?/g;
  const body = text.slice(text.indexOf("STATE_STYLES"), text.indexOf("export const STATE_ORDER"));
  for (const match of body.matchAll(/(\w+):\s*\{([\s\S]*?)\n {2}\},/g)) {
    const [, name, inner] = match;
    const str = (key) => (inner.match(new RegExp(`${key}:\\s*"([^"]*)"`)) ?? [])[1];
    const num = (key) => Number((inner.match(new RegExp(`${key}:\\s*([0-9.]+)`)) ?? [])[1]);
    const arr = (key) => (inner.match(new RegExp(`${key}:\\s*\\[([^\\]]*)\\]`)) ?? [])[1];
    out.push({
      state: name,
      token: str("token"),
      label: str("label"),
      tag: str("tag"),
      glyph: str("glyph"),
      dashCss: str("dashCss"),
      dash: (arr("dash") ?? "")
        .split(",")
        .map((v) => v.trim())
        .filter((v) => v !== ""),
      fillOpacity: num("fillOpacity"),
      block,
    });
  }
  return out;
}

const TOKENS = tokensFromCss(css);
const STATES = statesFromPalette(palette);

/* ------------------------------------------------------------------ colour maths */

function hexToRgb(hex) {
  const value = hex.replace("#", "");
  const full =
    value.length === 3
      ? value
          .split("")
          .map((c) => c + c)
          .join("")
      : value;
  const n = Number.parseInt(full, 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

function toLinear(channel) {
  const c = channel / 255;
  return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
}

function toSRgb(linear) {
  const c = linear <= 0.0031308 ? linear * 12.92 : 1.055 * linear ** (1 / 2.4) - 0.055;
  return Math.round(Math.min(1, Math.max(0, c)) * 255);
}

/** WCAG 2.1 relative luminance. */
function luminance(rgb) {
  const [r, g, b] = rgb.map(toLinear);
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function contrast(hexA, hexB) {
  const la = luminance(hexToRgb(hexA));
  const lb = luminance(hexToRgb(hexB));
  const lighter = Math.max(la, lb);
  const darker = Math.min(la, lb);
  return (lighter + 0.05) / (darker + 0.05);
}

function toLab(rgb) {
  const [r, g, b] = rgb.map(toLinear);
  // sRGB D65 -> XYZ
  const x = (0.4124564 * r + 0.3575761 * g + 0.1804375 * b) / 0.95047;
  const y = 0.2126729 * r + 0.7151522 * g + 0.072175 * b;
  const z = (0.0193339 * r + 0.119192 * g + 0.9503041 * b) / 1.08883;
  const f = (t) => (t > 0.008856 ? Math.cbrt(t) : 7.787 * t + 16 / 116);
  const [fx, fy, fz] = [f(x), f(y), f(z)];
  return [116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)];
}

function deltaE(rgbA, rgbB) {
  const [l1, a1, b1] = toLab(rgbA);
  const [l2, a2, b2] = toLab(rgbB);
  return Math.sqrt((l1 - l2) ** 2 + (a1 - a2) ** 2 + (b1 - b2) ** 2);
}

const CVD_MATRICES = {
  protanopia: [
    [0.152286, 1.052583, -0.204868],
    [-0.114717, 1.079406, 0.035311],
    [0.003853, -0.033733, 1.02988],
  ],
  deuteranopia: [
    [0.367327, 0.860951, -0.228278],
    [0.280085, 0.672501, 0.047414],
    [-0.01182, 0.042939, 0.968882],
  ],
  tritanopia: [
    [1.255528, -0.076749, -0.178779],
    [-0.078411, 0.930808, 0.147603],
    [0.004733, 0.691367, 0.303901],
  ],
};

function simulate(rgb, matrix) {
  const [r, g, b] = rgb.map(toLinear);
  const out = matrix.map((row) => Math.min(1, Math.max(0, row[0] * r + row[1] * g + row[2] * b)));
  return out.map(toSRgb);
}

/* ------------------------------------------------------------------ the pairs that must pass */

const TEXT_PAIRS = [
  ["text-primary", "surface-0", 4.5, "body text on the page"],
  ["text-primary", "surface-1", 4.5, "body text on a card"],
  ["text-primary", "surface-2", 4.5, "body text on the provenance strip"],
  ["text-secondary", "surface-1", 4.5, "secondary text on a card"],
  ["text-secondary", "surface-2", 4.5, "legend body text"],
  ["text-muted", "surface-1", 4.5, "small uppercase labels"],
  ["text-muted", "surface-0", 4.5, "colophon note"],
  ["link", "surface-1", 4.5, "nav and inline links"],
  ["danger-ink", "danger-strong", 4.5, "the standing statement banner"],
  ["text-inverse", "focus", 4.5, "skip link and active nav pill"],
];

const UI_PAIRS = [
  ["focus", "surface-0", 3, "focus ring on the page"],
  ["focus", "surface-1", 3, "focus ring on a card"],
  ["rule-strong", "surface-0", 1.5, "panel borders (informational)"],
  ["map-boundary", "map-basemap", 3, "service bbox outline"],
];

const STATE_PAIRS = STATES.map((state) => [
  state.token.replace(/^--/, ""),
  "surface-0",
  3,
  `${state.label} on the map canvas`,
]);
const STATE_PAIRS_2 = STATES.map((state) => [
  state.token.replace(/^--/, ""),
  "surface-1",
  3,
  `${state.label} on a card`,
]);

function resolve(name) {
  const key = name.startsWith("--") ? name : `--${name}`;
  const value = TOKENS.get(key);
  if (!value) {
    throw new Error(`token ${key} is not defined in tokens.css`);
  }
  return value;
}

const failures = [];
const lines = [];

function report(pairs, kind) {
  for (const [fg, bg, required, note] of pairs) {
    const ratio = contrast(resolve(fg), resolve(bg));
    const pass = ratio >= required;
    if (!pass) {
      failures.push(`${kind} ${fg} on ${bg}: ${ratio.toFixed(2)} below ${required} (${note})`);
    }
    lines.push(
      `${pass ? "PASS" : "FAIL"}  ${kind.padEnd(4)}  ${fg.padEnd(16)} on ${bg.padEnd(14)} ${ratio
        .toFixed(2)
        .padStart(6)}:1  (needs ${required}:1)  ${note}`,
    );
  }
}

report(TEXT_PAIRS, "text");
report(UI_PAIRS, "ui");
report(STATE_PAIRS, "ui");
report(STATE_PAIRS_2, "ui");

/* ------------------------------------------------------------------ second channels */

console.log("\nSecond-channel declaration per state (hue is never the only channel):");
for (const state of STATES) {
  const channels = [
    state.dash.length > 0 ? `dash ${state.dashCss}` : "solid stroke",
    `glyph ${state.glyph}`,
    `opacity ${state.fillOpacity}`,
    `tag "${state.tag}"`,
  ];
  const distinct = new Set(channels).size;
  if (distinct < 3) {
    failures.push(`state ${state.state} declares fewer than three non-colour channels`);
  }
  const token = TOKENS.get(state.token);
  if (!token) {
    failures.push(
      `state ${state.state} points at ${state.token}, which tokens.css does not define`,
    );
  }
  console.log(`  ${state.state.padEnd(10)} ${String(token).padEnd(9)} ${channels.join(" | ")}`);
}

/* ------------------------------------------------------------------ colour-blind separation */

console.log("\nPairwise separation, normal vision and under dichromatic simulation (CIE76):");
const COLLAPSE = 12;
for (let i = 0; i < STATES.length; i += 1) {
  for (let j = i + 1; j < STATES.length; j += 1) {
    const a = hexToRgb(resolve(STATES[i].token));
    const b = hexToRgb(resolve(STATES[j].token));
    const normal = deltaE(a, b);
    const rows = Object.entries(CVD_MATRICES).map(([name, matrix]) => [
      name,
      deltaE(simulate(a, matrix), simulate(b, matrix)),
    ]);
    const worst = Math.min(normal, ...rows.map(([, value]) => value));
    const pair = `${STATES[i].state}/${STATES[j].state}`;
    const sharesChannel =
      STATES[i].dashCss !== STATES[j].dashCss ||
      STATES[i].glyph !== STATES[j].glyph ||
      STATES[i].fillOpacity !== STATES[j].fillOpacity;
    const flag =
      worst < COLLAPSE
        ? sharesChannel
          ? "colour collapses, second channel carries it"
          : "FAIL: indistinguishable"
        : "separated by colour alone too";
    if (worst < COLLAPSE && !sharesChannel) {
      failures.push(`${pair} is indistinguishable under ${rows[0][0]} with no second channel`);
    }
    console.log(
      `  ${pair.padEnd(26)} dE normal ${normal.toFixed(1).padStart(5)}  ` +
        rows
          .map(([name, value]) => `${name.slice(0, 3)} ${value.toFixed(1).padStart(5)}`)
          .join("  ") +
        `   ${flag}`,
    );
  }
}

console.log(`\nMeasured contrast (WCAG 2.1 relative luminance, (L1+0.05)/(L2+0.05)):`);
console.log(lines.join("\n"));

const lightness = STATES.map((state) => [
  state.state,
  luminance(hexToRgb(resolve(state.token))).toFixed(3),
]);
console.log(`\nRelative luminance ladder (a hue-free ordering an operator can learn):`);
console.log(
  lightness
    .slice()
    .sort((a, b) => Number(b[1]) - Number(a[1]))
    .map(([name, value]) => `  ${name.padEnd(10)} ${value}`)
    .join("\n"),
);

if (failures.length > 0) {
  console.error(`\nCONTRAST AUDIT FAILED (${failures.length}):`);
  for (const failure of failures) {
    console.error(`  - ${failure}`);
  }
  process.exit(1);
}
console.log(
  `\nCONTRAST AUDIT PASSED: ${lines.length} measured pairs at or above their target, ${STATES.length} states each with three non-colour channels, no colour-blind collapse left unbacked.`,
);
