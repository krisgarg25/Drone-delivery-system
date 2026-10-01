/**
 * The map view: MapLibre for the camera and the offline dark canvas, deck.gl for the nine
 * data layers.
 *
 * Nothing is fetched over the network at runtime. The contract's `servers:` entry is a
 * reserved `.invalid` host and CSP allows `img-src 'self' data:`, so the basemap is drawn
 * from the frozen service bbox - which is also the honest choice: this product has no
 * tile licence and no live airspace source.
 *
 * Two rules are structural here, not stylistic:
 *   - a data state is never communicated by an animation. The uncertainty ring is static
 *     in every frame, and `reducedMotion` removes even the pan/zoom easing;
 *   - the canvas has a text twin (`#map-summary`) carrying the same layer counts and the
 *     same selected feature, so the information is not locked inside pixels.
 *
 * Simulation only. Not authorised for flight.
 */

import { MapboxOverlay } from "@deck.gl/mapbox";
import { LineLayer, PathLayer, PolygonLayer, ScatterplotLayer, TextLayer } from "@deck.gl/layers";
import type { Layer as DeckLayer } from "@deck.gl/core";
import maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";

import { STATE_STYLES, stateColor, stateRgb, token } from "../lib/palette";
import type { DataState } from "../lib/api/types";
import { LAYER_DEFS, LAYER_IDS, SYNTHETIC_CORRIDOR_LABEL } from "./layers";
import type { BuiltLayers, LayerId, MapDataset } from "./layers";
import { WORLD, SERVICE_BBOX } from "./world";
import { prefersReducedMotion } from "../lib/a11y";

interface Picked {
  title: string;
  detail: string;
  state: DataState;
  layer: LayerId;
}

export interface MapViewOptions {
  /** Structurally the same shape as the shell's LayerController, kept loose so the map
   * never imports the shell. */
  layers: {
    all(): readonly LayerId[];
    visible(id: LayerId): boolean;
    toggle(id: LayerId): void;
    subscribe(listener: (visible: ReadonlySet<LayerId>) => void): () => void;
  };
  announce(message: string): void;
}

export function createMapView(options: MapViewOptions): {
  mount(host: HTMLElement): void;
  update(dataset: MapDataset, built: BuiltLayers): void;
  destroy(): void;
} {
  let map: maplibregl.Map | null = null;
  let overlay: MapboxOverlay | null = null;
  let built: BuiltLayers = {
    lines: [],
    points: [],
    areas: [],
    counts: {} as BuiltLayers["counts"],
  };
  let visible: ReadonlySet<LayerId> = new Set(options.layers.all());
  let hovered: Picked | null = null;
  let summary: HTMLElement | null = null;
  let tip: HTMLElement | null = null;
  let unsubscribe: (() => void) | null = null;

  function reduced(): boolean {
    return prefersReducedMotion();
  }

  function layersFor(): DeckLayer[] {
    const show = (id: LayerId) => visible.has(id);
    const out: DeckLayer[] = [];

    // 1. basemap: service bbox fill, grid, and the UT boundary-ish mask.
    if (show("basemap")) {
      const [west, south, east, north] = SERVICE_BBOX;
      const grid: [number, number][][] = [];
      const step = 0.02;
      for (let lon = west; lon <= east + 1e-9; lon += step) {
        grid.push([
          [lon, south],
          [lon, north],
        ]);
      }
      for (let lat = south; lat <= north + 1e-9; lat += step) {
        grid.push([
          [west, lat],
          [east, lat],
        ]);
      }
      out.push(
        new PolygonLayer({
          id: "basemap-plane",
          data: [[west, south, east, north]],
          getPolygon: (bbox: number[]) => [
            [bbox[0], bbox[1]],
            [bbox[2], bbox[1]],
            [bbox[2], bbox[3]],
            [bbox[0], bbox[3]],
          ],
          filled: true,
          getFillColor: hexToRgba(token("--map-land"), 1),
          stroked: true,
          getLineColor: hexToRgba(token("--map-boundary"), 1),
          lineWidthMinPixels: 2,
        }),
        new PathLayer({
          id: "basemap-grid",
          data: grid,
          getPath: (line: [number, number][]) => line,
          getColor: hexToRgba(token("--map-grid"), 1),
          widthMinPixels: 1,
          widthMaxPixels: 1,
          jointRounded: false,
        }),
      );
    }

    // 2..9. everything drawn from the built model, grouped by (layer, state) so the dash
    // and the colour come from the same token table the legend prints.
    const lineGroups = groupBy(
      [...built.lines].filter((line) => show(line.layer)),
      (line) => `${line.layer}:${line.state}:${line.kind}`,
    );
    for (const [key, items] of lineGroups) {
      const state = key.split(":")[1] as DataState;
      const isShaft = items[0]?.kind === "arrow";
      out.push(
        new PathLayer({
          id: `lines-${key}`,
          data: items,
          getPath: (item: (typeof items)[number]) => item.coordinates,
          getColor: stateRgb(state),
          getWidth: (item: (typeof items)[number]) => item.widthPx,
          widthMinPixels: 1,
          capRounded: true,
          jointRounded: true,
          getDashArray: (item: (typeof items)[number]) => [...item.dash, 0, 0].slice(0, 4),
          pickable: true,
          autoHighlight: true,
          highlightColor: [255, 224, 102, 120],
          updateTriggers: { getDashArray: state },
        }),
      );
      if (isShaft) {
        out.push(
          new ScatterplotLayer({
            id: `shaft-head-${key}`,
            data: items,
            getPosition: (item: (typeof items)[number]) =>
              item.coordinates[item.coordinates.length - 1],
            getFillColor: stateRgb(state),
            radiusMinPixels: 3,
            radiusMaxPixels: 5,
            stroked: false,
            pickable: false,
          }),
        );
      }
    }

    if (show("weather-overlay")) {
      const labelled = built.lines.filter((line) => line.layer === "weather-overlay");
      out.push(
        new LineLayer({
          id: "weather-annotation",
          data: labelled,
          getSourcePosition: (item: (typeof labelled)[number]) => item.coordinates[0],
          getTargetPosition: (item: (typeof labelled)[number]) =>
            offset(item.coordinates[item.coordinates.length - 1]),
          getColor: stateRgb("synthetic"),
          getWidth: 1,
        }),
      );
    }

    const areaLayer = show("uncertainty-volume") || show("modelled-restriction");
    if (areaLayer) {
      const areas = built.areas.filter((area) => visible.has(area.layer));
      out.push(
        new PolygonLayer({
          id: "areas",
          data: areas,
          getPolygon: (item: (typeof areas)[number]) => item.polygon,
          extruded: true,
          elevationScale: 1,
          getElevation: (item: (typeof areas)[number]) => item.elevationM,
          filled: true,
          getFillColor: (item: (typeof areas)[number]) =>
            stateRgbWithAlpha(item.state, item.opacity),
          stroked: true,
          wireframe: true,
          getLineColor: (item: (typeof areas)[number]) => stateRgb(item.state),
          lineWidthMinPixels: 2,
          pickable: true,
          autoHighlight: true,
          highlightColor: [255, 224, 102, 120],
        }),
        // The second channel for an area is the boundary dash, drawn flat on the ground
        // so it survives any camera pitch.
        new PathLayer({
          id: "areas-hatch",
          data: areas,
          getPath: (item: (typeof areas)[number]) => item.polygon,
          getColor: (item: (typeof areas)[number]) => stateRgb(item.state),
          getWidth: 2,
          widthMinPixels: 2,
          getDashArray: (item: (typeof areas)[number]) => [...item.dash, 0, 0].slice(0, 4),
          pickable: false,
        }),
      );
    }

    const pointLayers = LAYER_IDS.filter(
      (id) => id !== "basemap" && id !== "weather-overlay" && visible.has(id),
    );
    const points = built.points.filter((point) => pointLayers.includes(point.layer));
    if (points.length > 0) {
      out.push(
        new ScatterplotLayer({
          id: "points",
          data: points,
          getPosition: (item: (typeof points)[number]) => item.coordinates,
          getFillColor: (item: (typeof points)[number]) =>
            stateRgbWithAlpha(item.state, STATE_STYLES[item.state].fillOpacity),
          getLineColor: (item: (typeof points)[number]) => stateRgb(item.state),
          lineWidthMinPixels: 2,
          radiusMinPixels: 4,
          radiusMaxPixels: 16,
          getRadius: (item: (typeof points)[number]) => item.radiusPx,
          stroked: true,
          pickable: true,
          autoHighlight: true,
          highlightColor: [255, 224, 102, 120],
        }),
        // Shape: the glyph is the same character the legend prints, so the second channel
        // is visible on the map and not only in the key.
        new TextLayer({
          id: "point-glyphs",
          data: points.filter((point) => point.radiusPx > 0.1),
          getPosition: (item: (typeof points)[number]) => item.coordinates,
          getText: (item: (typeof points)[number]) => item.glyph,
          getColor: (item: (typeof points)[number]) => stateRgb(item.state),
          sizeUnits: "pixels",
          getSize: 11,
          characterSet: "auto",
          textAnchor: "middle",
          textAlignment: "center",
          pickable: false,
        }),
        new TextLayer({
          id: "point-labels",
          data: points.filter((point) => point.label !== undefined),
          getPosition: (item: (typeof points)[number]) => item.coordinates,
          getText: (item: (typeof points)[number]) =>
            `${item.label ?? ""} ${STATE_STYLES[item.state].tag}`,
          getColor: hexToRgba(token("--text-secondary"), 1),
          sizeUnits: "pixels",
          getSize: 10,
          characterSet: "auto",
          pickable: false,
        }),
      );
    }

    return out;
  }

  function render(): void {
    if (overlay) {
      overlay.setProps({ layers: layersFor() });
    }
    renderSummary();
  }

  function renderSummary(): void {
    if (!summary) {
      return;
    }
    const rows = LAYER_DEFS.map((def) => {
      const count = built.counts[def.id];
      const state = def.states
        .map((key) => `${STATE_STYLES[key].label} ${STATE_STYLES[key].tag}`)
        .join(", ");
      return `${def.label}: ${count} feature${count === 1 ? "" : "s"}${
        state === "" ? "" : `, channel ${state}`
      }${visible.has(def.id) ? "" : " (hidden)"}`;
    });
    summary.textContent =
      rows.join(". ") +
      `. ${hovered ? `Selected feature: ${hovered.title}. ${hovered.detail.replace(/\n/g, " ")}` : "No feature selected. Tab to the map and use the arrow keys to pan, plus and minus to zoom."}`;
  }

  function describe(info: { object?: unknown; x?: number; y?: number } | null): void {
    const object = info?.object as (Picked & { coordinates?: unknown }) | undefined;
    hovered =
      object && typeof object.title === "string"
        ? { title: object.title, detail: object.detail, state: object.state, layer: object.layer }
        : null;
    if (!tip || !map || !info) {
      if (tip) {
        tip.hidden = true;
      }
      renderSummary();
      return;
    }
    if (!hovered) {
      tip.hidden = true;
      renderSummary();
      return;
    }
    tip.hidden = false;
    tip.textContent = "";
    const heading = document.createElement("strong");
    heading.textContent = `${STATE_STYLES[hovered.state].glyph} ${hovered.title}`;
    const body = document.createElement("span");
    body.textContent = hovered.detail;
    tip.append(heading, document.createElement("br"), body);
    tip.style.left = `${(info.x ?? 0) + 12}px`;
    tip.style.top = `${(info.y ?? 0) + 12}px`;
    renderSummary();
  }

  return {
    mount(host: HTMLElement) {
      host.textContent = "";
      host.setAttribute("role", "region");
      host.setAttribute("aria-label", "Reservations map. All geometry is a synthetic model.");

      // The canvas and its chrome live in .map-root; the legend and the text twin are
      // siblings, so neither is clipped by the canvas overflow and both stay reachable
      // with the keyboard.
      const stage = document.createElement("div");
      stage.className = "map-root";

      const canvas = document.createElement("div");
      canvas.id = "map-canvas";
      canvas.className = "map-canvas";
      canvas.tabIndex = 0;
      canvas.setAttribute(
        "aria-label",
        "Map canvas. Arrow keys pan, plus and minus zoom. Every layer has a text equivalent below.",
      );

      const badge = document.createElement("p");
      badge.className = "map-model-badge";
      badge.textContent = `Synthetic corridor model - ${SYNTHETIC_CORRIDOR_LABEL}`;

      const attribution = document.createElement("p");
      attribution.className = "map-attribution";
      attribution.textContent = `${WORLD.provenance.attribution}. No live tiles, no live airspace, no official buffers. ${WORLD.notice}`;

      tip = document.createElement("div");
      tip.className = "map-tip";
      tip.setAttribute("role", "tooltip");
      tip.hidden = true;

      summary = document.createElement("div");
      summary.id = "map-summary";
      summary.className = "map-summary";
      summary.setAttribute("aria-live", "polite");
      summary.setAttribute("tabindex", "-1");

      const legendHost = document.createElement("section");
      legendHost.className = "legend";
      legendHost.setAttribute("aria-labelledby", "legend-heading");
      legendHost.id = "map-legend";

      stage.append(canvas, badge, attribution, tip);
      host.append(stage, summary, legendHost);
      renderLegend(legendHost, options, () => visible, render);

      const [west, south, east, north] = SERVICE_BBOX;
      map = new maplibregl.Map({
        container: canvas,
        center: [(west + east) / 2, (south + north) / 2],
        zoom: 11.4,
        minZoom: 9,
        maxZoom: 16,
        maxBounds: [
          [west - 0.06, south - 0.06],
          [east + 0.06, north + 0.06],
        ],
        attributionControl: false,
        dragRotate: false,
        style: {
          version: 8,
          sources: {},
          layers: [
            {
              id: "background",
              type: "background",
              paint: { "background-color": token("--map-basemap") },
            },
          ],
        },
      });
      map.addControl(new maplibregl.NavigationControl({ visualizePitch: false }), "top-left");
      map.addControl(new maplibregl.AttributionControl({ compact: false }), "bottom-left");
      map.on("load", () => {
        if (!map) {
          return;
        }
        map.setPitch(38);
        if (reduced()) {
          // No easing anywhere near a data state: under reduced motion the camera has no
          // inertial or animated zoom, so nothing glides a uncertainty ring into place.
          map.scrollZoom.disable();
          map.doubleClickZoom.disable();
          map.boxZoom.disable();
        }
        overlay = new MapboxOverlay({
          interleaved: false,
          layers: layersFor(),
          onHover: (info) => {
            describe(info as { object?: unknown; x?: number; y?: number });
          },
          onClick: (info) => {
            describe(info as { object?: unknown; x?: number; y?: number });
            options.announce(
              hovered
                ? `${hovered.title}. ${hovered.detail.split("\n")[0]}`
                : "No feature at this point.",
            );
          },
          // The tooltip is ours (`describe` below): it has to carry the state tag and the
          // provenance line, which deck's default string tooltip cannot.
        });
        map.addControl(overlay as unknown as maplibregl.IControl);
        render();
      });
      unsubscribe = options.layers.subscribe((next) => {
        visible = next;
        render();
      });
      visible = new Set(options.layers.all().filter((id) => options.layers.visible(id)));
      render();
    },
    update(dataset: MapDataset, next: BuiltLayers) {
      void dataset;
      built = next;
      render();
    },
    destroy() {
      unsubscribe?.();
      overlay?.finalize();
      map?.remove();
      map = null;
      overlay = null;
    },
  };
}

/* ------------------------------------------------------------------ legend */

function renderLegend(
  host: HTMLElement,
  options: MapViewOptions,
  visibleNow: () => ReadonlySet<LayerId>,
  onChange: () => void,
): void {
  const heading = document.createElement("h3");
  heading.id = "legend-heading";
  heading.textContent = "Legend and layers";
  const intro = document.createElement("p");
  intro.className = "legend-intro";
  intro.textContent =
    "Each state has two independent channels: a colour and a pattern, shape, opacity or word. Colour alone never carries meaning.";
  host.append(heading, intro);

  const list = document.createElement("ul");
  list.className = "legend-list";
  for (const [state, style] of Object.entries(STATE_STYLES)) {
    const item = document.createElement("li");
    item.className = "legend-item";
    item.dataset.state = state;
    const swatch = document.createElement("span");
    swatch.className = "legend-swatch";
    swatch.setAttribute("aria-hidden", "true");
    swatch.style.setProperty("--swatch", stateColor(style.state, 1));
    swatch.style.setProperty("--swatch-dash", style.dashCss);
    swatch.textContent = style.glyph;
    const text = document.createElement("span");
    const label = document.createElement("strong");
    label.textContent = style.label;
    const tagEl = document.createElement("span");
    tagEl.className = "legend-tag";
    tagEl.textContent = style.tag;
    const meaning = document.createElement("span");
    meaning.className = "legend-meaning";
    meaning.textContent = `${style.meaning} ${style.notThis}`;
    text.append(label, tagEl, meaning);
    const dash = document.createElement("span");
    dash.className = "legend-dash";
    dash.textContent = `pattern: ${style.dashCss === "none" ? "solid" : style.dashCss}`;
    item.append(swatch, text, dash);
    list.append(item);
  }
  host.append(list);

  const fieldset = document.createElement("fieldset");
  fieldset.className = "layer-toggles";
  const legend = document.createElement("legend");
  legend.textContent = "Map layers";
  fieldset.append(legend);
  for (const def of LAYER_DEFS) {
    const label = document.createElement("label");
    label.className = "layer-toggle";
    const input = document.createElement("input");
    input.type = "checkbox";
    input.checked = visibleNow().has(def.id);
    input.addEventListener("change", () => {
      options.layers.toggle(def.id);
      const next = visibleNow();
      input.checked = next.has(def.id);
      onChange();
      options.announce(`${def.label} ${next.has(def.id) ? "shown" : "hidden"}`);
    });
    const text = document.createElement("span");
    const name = document.createElement("strong");
    name.textContent = `${def.order}. ${def.label}`;
    const source = document.createElement("span");
    source.className = "layer-source";
    source.textContent = def.source;
    const channel = document.createElement("span");
    channel.className = "layer-channel";
    channel.textContent = `Channel: ${def.channel}`;
    const warning = document.createElement("span");
    warning.className = "layer-warning";
    warning.textContent = def.warning;
    text.append(name, source, channel, warning);
    label.append(input, text);
    fieldset.append(label);
  }
  host.append(fieldset);
}

/* ------------------------------------------------------------------ helpers */

export function groupBy<T, K extends string>(
  items: readonly T[],
  key: (item: T) => K,
): Map<K, T[]> {
  const out = new Map<K, T[]>();
  for (const item of items) {
    const k = key(item);
    const bucket = out.get(k);
    if (bucket) {
      bucket.push(item);
    } else {
      out.set(k, [item]);
    }
  }
  return out;
}

function hexToRgba(hex: string, alpha: number): [number, number, number, number] {
  const value = hex.replace("#", "");
  const n = Number.parseInt(value.length === 3 ? value.replace(/(.)/g, "$1$1") : value, 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255, Math.round(alpha * 255)];
}

function stateRgbWithAlpha(state: DataState, alpha: number): [number, number, number, number] {
  const [r, g, b] = stateRgb(state);
  return [r, g, b, Math.round(alpha * 255)];
}

function offset(point: [number, number]): [number, number] {
  return [point[0] + 0.0025, point[1] + 0.0025];
}
