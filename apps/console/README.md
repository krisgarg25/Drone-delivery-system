# DroneOps operations console

Phase 4 foundation: the shell, the design system, the contract-typed client and the map.
The queue, board, fleet, economics, decision-trace, replay and comparison panels mount into
this shell through one registry and are owned by whoever builds them next.

**Simulation only. Not authorised for flight.** The banner is in the document header and
footer, outside the route region, so no screen — including an error screen — can remove it.

## Run it

```bash
cd apps/console
npm ci
npm run dev        # http://127.0.0.1:5173, proxies /health /version /v1 to :8000
npm run build      # tsc --noEmit && vite build
npm run preview    # http://127.0.0.1:4173 (what the Playwright smoke test expects)
npm run lint       # eslint . && prettier --check .
```

With the control plane up (`make db-up && make api`), the console probes `/health` and uses
the live API. With nothing listening, it falls back to the synthetic dev transport and says
so in the header badge and the provenance strip. Force either: `?transport=live` or
`?transport=synthetic` - and `?transport=fixtures` is accepted as an alias of `synthetic`,
because that is the word the demo notes use for the mode. Both keywords are behind the same
`!import.meta.env.PROD` guard in `src/main.ts`, and the fixture transport is refused at
construction in a production build (`src/lib/api/mock.ts`), so it cannot ship.

## Types come from the contract

```bash
npm run generate:types   # openapi-typescript ../../contracts/openapi/v1.yaml -> src/generated/api-types.ts
npm run check:generated  # gate: present, newer than the contract, zero `any`, all 22 schemas
```

`src/generated/api-types.ts` is committed so a checkout builds without running the
generator. It is excluded from ESLint (nobody hand-writes it) but still type-checked, and
`check:generated` fails if the generated output contains `any`. Every payload type in the
app is projected out of it — `ResponseFor<"listReservations">`,
`Schemas["UncertaintyStateResponse"]`, `SourceKind` — so there is no hand-written duplicate
of a contract shape anywhere in `src/`. The only two non-generated payloads are `/health`
and `/version`, which are deliberately outside the reviewed business contract; they are
isolated and marked in `src/lib/api/types.ts`.

The client is read-only by construction: `READ_ROUTES` lists the 17 GET operations and the
9 mutations are named but unreachable, because the console presents and never decides
(`docs/conventions.md` section 7, plus the ESLint rule that blocks a
`reserve|commit|dispatch|launch|command` call in this package).

## Design system

`src/styles/tokens.css` is the source. `src/lib/palette.ts` reads those custom properties at
runtime, so the legend, the canvas and the audit script all measure the same values.

Six data states, each with a **second channel** — hue is never the only signal:

| state     | token                         | dash       | glyph | fill | tag         |
| --------- | ----------------------------- | ---------- | ----- | ---- | ----------- |
| committed | `--state-committed` `#f2b53b` | solid      | `■`   | 0.55 | `LEDGER`    |
| planned   | `--state-planned` `#4cc9f0`   | `6 4`      | `◌`   | 0.28 | `PLAN`      |
| observed  | `--state-observed` `#7ae582`  | `1 3`      | `▲`   | 0.90 | `MEASURED`  |
| uncertain | `--state-uncertain` `#dfe6ee` | `2 2`      | `◎`   | 0.18 | `± UNKNOWN` |
| rejected  | `--state-rejected` `#ff6b6b`  | `3 1`      | `✕`   | 0.75 | `REJECTED`  |
| synthetic | `--state-synthetic` `#b89bff` | `12 3 2 3` | `◆`   | 0.35 | `SYNTH`     |

`npm run audit:contrast` parses the token file and prints measured WCAG 2.1 ratios plus a
deuteranopia/protanopia/tritanopia separation check; it exits non-zero if a pair misses its
target. Current output (26 measured pairs, all passing):

```
text-primary   on surface-0  16.94:1 | surface-1 15.32:1 | surface-2 13.49:1
text-secondary on surface-1    8.12:1 | surface-2  7.15:1
text-muted     on surface-1    5.59:1 | surface-0  6.18:1
link           on surface-1   10.18:1
danger-ink     on banner       7.32:1
text-inverse   on focus ring  14.74:1
focus ring     on surface-0   14.50:1 (3:1 required)
bbox outline   on basemap      4.01:1 (3:1 required)
planned 9.83:1  committed 10.31:1  observed 12.03:1
synthetic 8.26:1  uncertain 15.03:1  rejected 6.81:1   (all on the map canvas, 3:1 required)
```

Relative luminance gives an order that survives total colour loss:
uncertain 0.785 > observed 0.618 > committed 0.522 > planned 0.496 > synthetic 0.409 >
rejected 0.328. The worst simulated separation is dE 12.8 (observed/rejected under
deuteranopia) and dE 13.0 (planned/synthetic) — both pairs differ in dash, glyph, opacity
and tag, so neither depends on hue. The legend on the map screen states every channel in
words.

Motion: `--motion-scale` collapses to `0` under `prefers-reduced-motion`, transitions are
decorative only, and no data state is ever introduced by an animation — the uncertainty ring
appears in place. Under reduced motion the map also drops inertial, box and double-click
zoom.

## Map

`src/map/map-view.ts` (MapLibre camera + deck.gl overlays) and `src/map/layers.ts` (the
model). The nine layers from `technical-build/11` section 12, in draw order, plus one:

1. basemap — frozen service bbox and grid. No tile server: the contract's host is a reserved
   `.invalid` and the CSP allows `img-src 'self' data:`.
2. corridor centreline — 22 scenario edges and 60 OSM-aligned candidates, labelled **"Synthetic
   corridor model (planning heuristic only, not a clearance assessment)"** in a permanent chip.
3. occupied reservation — committed ledger rows joined to geometry by `resource_key`.
4. proposed plan — plan versions, dashed, `PLAN`.
5. observed telemetry — the only coordinates the contract carries
   (`TelemetryObservationResponse.position`), dotted, `MEASURED`, out-of-order frames flagged.
6. uncertainty volume — extruded hatched ring plus the retained `unresolved` protected volume.
7. facility pads — fictional hubs and handover pads with pad/charger counts and API status.
8. rejected request markers — every rejected candidate with its human reason as map text.
9. weather overlay — the frozen wind trace; magnitude only, because the trace has no
   direction, and the tooltip says so.
10. modelled keep-out — a 1 500 m ring around a fictional airfield pad, labelled
    "NOT official airspace data" in its own name.

Hard rules enforced in the labels and tooltips, not in comments: no candidate node is named
after a real hospital or business (the bundle's facilities are "Fictional … Depot"),
corridors are never described as safe, and no buffer is presented as published airspace.

**What the contract cannot do.** It carries geometry for telemetry positions only. Facilities,
plans and reservations carry identifiers and `resource_key`s. So the corridor and pad geometry
comes from the frozen scenario bundle, copied by `npm run build:synthetic-data`
(`scripts/build_synthetic_dataset.py`, needs the repo venv for PyYAML) into
`src/map/data/synthetic-world.json` (40 KiB, committed), and ledger rows join onto it through
`corridor:` / `pad:` keys. A row that cannot be joined is listed as a marker that says it
cannot be joined — it is never drawn as an invented line.

## Provenance strip

Rendered on every screen, including error screens: scenario id, source (including which
transport answered), retrieval date with age, and the sha256 over the sorted bundle file
digests, plus `authoritative: false` and what the dataset does not prove. Weather carries its
own retrieval and expiry stamps. The bundle hashes are the ones the deterministic runner
records, so a replay reproduces the same strings (`make replay`).

## Panel seam — for the next engineer

1. Export a `Panel` (`id`, `slot`, `title`, `description`, `mount(host, context)`) from
   `src/panels/<name>.ts`; `src/shell/panels.ts` documents the `PanelContext` you get.
2. Add it to the `PANELS` array (or call `registerPanel`) against one of the eight typed
   slots — `missions.queue`, `missions.board`, `fleet.status`, `economics.metrics`,
   `decision.trace`, `replay.controls`, `comparison.runs`, `map.context`. The shell mounts it;
   nothing in `src/shell/` or `src/lib/` needs editing.
3. Render with `textContent`, use `context.stateChip(state)` so your colours and second
   channels come from the token table, report `source_kind` / `authoritative` /
   `meta.transportMode` as you receive them, register teardown with `context.onDispose`, and
   start your headings at `<h3>` — the shell owns `<h2>`.

## Accessibility

Skip link to the working area, `banner`/`navigation`/`main`/`contentinfo` landmarks, one `h1`
with route `h2` below it, `aria-current` on the live nav item, focus moved to the route
heading after every navigation with the destination announced through a polite live region,
a two-channel focus ring (bright outline plus dark halo), keyboard-operable layer toggles,
and `#map-summary`: a text twin carrying the same layer counts and the same selected feature
as the canvas, so nothing exists only in pixels.

## Layout

```
index.html                 shell markup: banner, provenance strip, nav, main, footer banner
src/main.ts                transport choice, bootstrap, fatal error screens (banner survives)
src/shell/                 shell.ts (routing, slots, data loading), routes.ts, panels.ts (seam)
src/lib/api/               client.ts (transports, ApiResult), endpoints.ts, mock.ts, types.ts
src/lib/                   palette.ts, provenance.ts, a11y.ts
src/map/                   map-view.ts, layers.ts, world.ts, data/synthetic-world.json
src/styles/                tokens.css, shell.css, map.css (aggregated by src/styles.css)
src/generated/api-types.ts openapi-typescript output, committed
scripts/                   audit-contrast.mjs, check-generated.mjs, build_synthetic_dataset.py
```

## What is not here

No scheduling, dispatch or reservation logic in the browser — none. No vehicle command path
exists anywhere in this repository, and the UI has no control that could be one (the Playwright
smoke test asserts the absence of launch/takeoff/land/RTL/arm/command/upload labels). No
marketplace, no mobile app, no live weather, no UTM integration, no 3D city effects
(`docs/conventions.md` section 13).
