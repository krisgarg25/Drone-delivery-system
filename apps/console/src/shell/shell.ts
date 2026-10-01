/**
 * The application shell: markup, landmarks, routing, the provenance strip, the
 * simulation-only banner, focus management and data loading. It renders; it never decides.
 *
 * Structure contract for the next engineer:
 *   header[role=banner].safety-banner   - the standing statement, always present
 *   .provenance-strip > dl#status-list  - scenario, source, retrieval date, hash, authority
 *   nav[role=navigation]                - the seven routes, aria-current on the live one
 *   main#main[role=main] > h2 route     - route heading; panels start at h3
 *   section.panel-slot[data-slot]       - the containers registered panels mount into
 *   footer[role=contentinfo].safety-banner
 * The banner is in the header and the footer of the document, outside the route region,
 * so an error page cannot remove it and a route change cannot flicker it.
 *
 * Simulation only. Not authorised for flight.
 */

import { announce, ensureLiveRegion, installSkipLink, prefersReducedMotion } from "../lib/a11y";
import { SIMULATION_NOTICE } from "../lib/api/client";
import type { ApiResult, ReadOperationId } from "../lib/api/client";
import type { Api } from "../lib/api/endpoints";
import type { DataState, Schemas } from "../lib/api/types";
import type { LayerId } from "../map/layers";
import { provenanceFields, shortHash } from "../lib/provenance";
import { STATE_STYLES } from "../lib/palette";
import { createMapView } from "../map/map-view";
import type { MapDataset } from "../map/layers";
import { buildLayers } from "../map/layers";
import { WORLD } from "../map/world";
import { DEFAULT_ROUTE, ROUTES, SLOT_LABELS, routeById, routeByHash } from "./routes";
import type { PanelSlot, RouteDefinition } from "./routes";
import { createLayerController, createSelectionStore, panelsForSlot } from "./panels";
import type { Disposer, PanelContext, Selection } from "./panels";

const EMPTY_DATASET: MapDataset = {
  facilities: [],
  reservations: [],
  plans: [],
  observations: [],
  uncertainty: null,
  decision: null,
  syntheticTransport: false,
};

export interface Shell {
  api: Api;
  context: PanelContext;
  start(): Promise<void>;
  destroy(): void;
}

export function createShell(api: Api): Shell {
  const layers = createLayerController([
    "basemap",
    "corridor-centerline",
    "occupied-reservation",
    "proposed-plan",
    "observed-telemetry",
    "uncertainty-volume",
    "facility-pads",
    "rejected-request",
    "weather-overlay",
    "modelled-restriction",
  ] as const satisfies readonly LayerId[]);
  const selection = createSelectionStore({
    missionId: null,
    aircraftId: null,
    corridorId: null,
    scenarioId: WORLD.provenance.scenario_id,
  } satisfies Selection);

  const mapView = createMapView({ layers, announce });

  let lastReceivedAt: string | undefined;
  let disposers: Disposer[] = [];
  let route: RouteDefinition = routeById(DEFAULT_ROUTE) ?? ROUTES[0];
  /** The route currently drawn in `main`, or null before the first render. */
  let renderedRoute: RouteDefinition | null = null;
  let dataset: MapDataset = EMPTY_DATASET;

  const context: PanelContext = {
    api,
    layers,
    selection,
    announce,
    reducedMotion: prefersReducedMotion,
    stateChip,
    onDispose(disposer) {
      disposers.push(disposer);
    },
  };

  function stateChip(state: DataState, text?: string): HTMLElement {
    const chip = document.createElement("span");
    chip.className = `state-chip state-chip--${state}`;
    chip.dataset.state = state;
    const glyph = document.createElement("span");
    glyph.setAttribute("aria-hidden", "true");
    glyph.className = "state-chip-glyph";
    glyph.textContent = STATE_STYLES[state].glyph;
    const label = document.createElement("span");
    label.textContent = text ?? STATE_STYLES[state].label;
    const tag = document.createElement("span");
    tag.className = "state-chip-tag";
    tag.textContent = STATE_STYLES[state].tag;
    chip.append(glyph, label, tag);
    chip.title = `${STATE_STYLES[state].meaning} ${STATE_STYLES[state].notThis}`;
    return chip;
  }

  /* ------------------------------------------------------------------ provenance strip */

  function renderProvenance(errors: readonly string[]): void {
    const strip = document.getElementById("status-list");
    if (!(strip instanceof HTMLDListElement)) {
      return;
    }
    strip.replaceChildren();
    const fields = provenanceFields(
      { mode: api.transport.mode, label: api.transport.label },
      lastReceivedAt,
    );
    const rows: [string, string][] = [
      ["api", api.transport.mode === "live" ? "live control plane" : "not contacted"],
      ["authorised for flight", "no"],
      ...fields.map((field) => [field.label, field.value] as [string, string]),
      ...errors.map((message) => ["request failed", message] as [string, string]),
    ];
    for (const [term, value] of rows) {
      const dt = document.createElement("dt");
      dt.textContent = term;
      const dd = document.createElement("dd");
      // textContent, never innerHTML: API strings stay data and cannot become markup.
      dd.textContent = value;
      strip.append(dt, dd);
    }
    const badge = document.getElementById("transport-badge");
    if (badge instanceof HTMLElement) {
      badge.dataset.mode = api.transport.mode;
      badge.textContent =
        api.transport.mode === "synthetic"
          ? "DEV FIXTURES - synthetic transport, not live data"
          : "live /v1 reads";
    }
  }

  /* ------------------------------------------------------------------ routes */

  function renderRouteShell(): void {
    const main = document.getElementById("main");
    if (!(main instanceof HTMLElement)) {
      return;
    }
    main.replaceChildren();

    const heading = document.createElement("h2");
    heading.id = "route-heading";
    heading.textContent = route.label;
    const summary = document.createElement("p");
    summary.className = "route-summary";
    summary.textContent = route.summary;
    main.append(heading, summary);

    if (route.id === "map") {
      const mapRegion = document.createElement("section");
      mapRegion.className = "map-region";
      mapRegion.id = "map-root";
      mapRegion.setAttribute("aria-labelledby", "legend-heading");
      main.append(mapRegion);
      mapView.mount(mapRegion);
    }

    for (const slot of route.slots) {
      main.append(renderSlot(slot));
    }
  }

  function renderSlot(slot: PanelSlot): HTMLElement {
    const section = document.createElement("section");
    section.className = "panel-slot";
    section.dataset.slot = slot;
    section.setAttribute("aria-labelledby", `slot-${slot}`);
    const heading = document.createElement("h3");
    heading.id = `slot-${slot}`;
    heading.textContent = SLOT_LABELS[slot];
    const host = document.createElement("div");
    host.className = "panel-host";
    host.id = `panel-host-${slot}`;
    section.append(heading, host);

    const panels = panelsForSlot(slot);
    if (panels.length === 0) {
      const empty = document.createElement("p");
      empty.className = "panel-empty";
      empty.textContent =
        `No panel is registered for the "${slot}" slot yet. ` +
        "Export a Panel from src/panels/ and add it to PANELS in src/shell/panels.ts - " +
        "the shell mounts it here without any further change.";
      host.append(empty);
    }
    return section;
  }

  function disposePanels(): void {
    for (const dispose of disposers.reverse()) {
      dispose();
    }
    disposers = [];
  }

  function mountPanels(): void {
    disposePanels();
    for (const slot of route.slots) {
      const host = document.getElementById(`panel-host-${slot}`);
      if (!(host instanceof HTMLElement)) {
        continue;
      }
      for (const panel of panelsForSlot(slot)) {
        host.append(panelChrome(panel.title, panel.description));
        const inner = document.createElement("div");
        inner.className = "panel-body";
        inner.dataset.panel = panel.id;
        host.append(inner);
        try {
          panel.mount(inner, context);
        } catch (cause) {
          inner.replaceChildren(errorNote(String(cause)));
        }
      }
    }
  }

  function panelChrome(title: string, description: string): HTMLElement {
    const wrapper = document.createElement("p");
    wrapper.className = "panel-title";
    const strong = document.createElement("strong");
    strong.textContent = title;
    wrapper.append(strong, document.createTextNode(` ${description}`));
    return wrapper;
  }

  function errorNote(message: string): HTMLElement {
    const note = document.createElement("p");
    note.className = "panel-error";
    note.setAttribute("role", "alert");
    note.textContent = `This panel failed to render: ${message}. The rest of the console still works, and nothing here was computed in the browser.`;
    return note;
  }

  function navigate(next: RouteDefinition, focusHeading = true): void {
    const previous = renderedRoute;
    route = next;
    if (previous !== next) {
      if (previous !== null && previous.id === "map") {
        // The map canvas is expensive and owns the camera; it is torn down only when the
        // operator leaves the map screen.
        mapView.destroy();
      }
      renderRouteShell();
      if (next.id === "map") {
        mapView.update(dataset, buildLayers(dataset));
      }
      renderedRoute = next;
    }
    for (const link of document.querySelectorAll<HTMLAnchorElement>("nav a[href^='#/']")) {
      const target = link.getAttribute("href") ?? "";
      if (target === next.path) {
        link.setAttribute("aria-current", "page");
      } else {
        link.removeAttribute("aria-current");
      }
    }
    document.title = `${next.label} - DroneOps console - Simulation only`;
    mountPanels();
    if (focusHeading) {
      const heading = document.getElementById("route-heading");
      if (heading instanceof HTMLElement) {
        heading.tabIndex = -1;
        heading.focus();
      }
      announce(`${next.label} screen. ${next.summary}`);
    }
    history.replaceState(null, "", next.path);
  }

  function onHashChange(): void {
    const next = routeByHash(location.hash);
    if (!next) {
      renderNotFound(location.hash);
      return;
    }
    try {
      navigate(next);
    } catch (cause) {
      renderRouteError(String(cause));
    }
  }

  function renderNotFound(hash: string): void {
    const main = document.getElementById("main");
    if (!(main instanceof HTMLElement)) {
      return;
    }
    main.replaceChildren();
    const heading = document.createElement("h2");
    heading.id = "route-heading";
    heading.textContent = "Screen not found";
    const body = document.createElement("p");
    body.setAttribute("role", "alert");
    body.textContent = `No screen is registered at ${hash || "this address"}. The console has exactly seven: ${ROUTES.map((entry) => entry.label).join(", ")}.`;
    const back = document.createElement("a");
    back.className = "back-link";
    back.href = ROUTES[0].path;
    back.textContent = "Open the map";
    main.append(heading, body, back);
    heading.tabIndex = -1;
    heading.focus();
    announce("Screen not found. The console has seven screens.");
  }

  function renderRouteError(message: string): void {
    const main = document.getElementById("main");
    if (!(main instanceof HTMLElement)) {
      return;
    }
    main.replaceChildren();
    const heading = document.createElement("h2");
    heading.id = "route-heading";
    heading.textContent = "This screen could not be drawn";
    const body = document.createElement("p");
    body.setAttribute("role", "alert");
    body.textContent = `${message}. The banner above still applies: this console is a simulation and authorises nothing.`;
    main.append(heading, body);
    heading.tabIndex = -1;
    heading.focus();
  }

  /* ------------------------------------------------------------------ data */

  /**
   * Which mission is on the map is data, not a constant: ask the order book and fall back
   * to the operator's explicit selection. With an empty ledger the per-mission requests are
   * skipped and the strip says so, instead of firing four 404s at a mission that is not there.
   */
  async function discoverMission(errors: string[]): Promise<string | null> {
    const orders = await api.orders();
    if (!orders.ok) {
      errors.push(`${orders.error.operation}: ${orders.error.message}`);
      return null;
    }
    lastReceivedAt = orders.meta.receivedAt;
    const ids = orders.data.items.map((order) => order.mission_id);
    return ids.length > 0 ? ids[0] : null;
  }

  async function loadMapData(): Promise<void> {
    const errors: string[] = [];
    const mission = selection.get().missionId ?? (await discoverMission(errors));
    /**
     * Six slots, always requested. With no mission in scope the four per-mission slots
     * resolve to an explicit "not requested" result rather than being skipped, so the
     * provenance strip names the gap instead of the UI looking silently empty.
     */
    const notRequested = (operation: ReadOperationId): ApiResult<never> => ({
      ok: false,
      error: {
        kind: "contract",
        message: "no mission is in scope, so this was not requested",
        operation,
      },
    });
    const requests: Promise<ApiResult<unknown>>[] = [
      api.facilities(),
      api.reservations(WINDOW_FROM, WINDOW_TO),
      mission === null
        ? Promise.resolve(notRequested("listMissionPlanVersions"))
        : api.planVersions(mission),
      mission === null
        ? Promise.resolve(notRequested("listMissionObservations"))
        : api.observations(mission),
      mission === null
        ? Promise.resolve(notRequested("getMissionUncertaintyState"))
        : api.uncertainty(mission),
      mission === null
        ? Promise.resolve(notRequested("getMissionDecisionTrace"))
        : api.decisionTrace(mission),
    ];
    const results: (PromiseSettledResult<ApiResult<unknown>> | undefined)[] =
      await Promise.allSettled(requests);
    const take = <T>(index: number, fallback: T): T => {
      const result = results[index];
      if (result === undefined || result.status === "rejected") {
        errors.push(
          `request ${index} threw: ${result === undefined ? "no slot" : String(result.reason)}`,
        );
        return fallback;
      }
      const value = result.value;
      if (value.ok) {
        lastReceivedAt = value.meta.receivedAt;
        return value.data as T;
      }
      errors.push(`${value.error.operation}: ${value.error.message}`);
      return fallback;
    };

    const facilities = take<Schemas["FacilityResponseList"]>(0, { items: [], ...PROV }).items;
    const reservations = take<Schemas["ReservationListResponse"]>(1, { items: [], ...PROV }).items;
    const plans = take<Schemas["PlanVersionResponseList"]>(2, { items: [], ...PROV }).items;
    const observations = take<Schemas["TelemetryListResponse"]>(3, { items: [], ...PROV }).items;
    const uncertainty = take<Schemas["UncertaintyStateResponse"] | null>(4, null);
    const decision = take<Schemas["DecisionTraceResponse"] | null>(5, null);

    dataset = {
      facilities,
      reservations,
      plans,
      observations,
      uncertainty,
      decision,
      syntheticTransport: api.transport.mode === "synthetic",
    };
    mapView.update(dataset, buildLayers(dataset));
    renderProvenance(errors);
    if (errors.length > 0) {
      announce(`${errors.length} requests failed. ${errors[0]}`);
    }
  }

  return {
    api,
    context,
    async start() {
      ensureLiveRegion();
      installSkipLink("main");
      const nav = document.getElementById("primary-nav");
      if (nav instanceof HTMLElement && nav.childElementCount === 0) {
        const list = document.createElement("ul");
        for (const entry of ROUTES) {
          const item = document.createElement("li");
          const link = document.createElement("a");
          link.href = entry.path;
          link.textContent = entry.label;
          item.append(link);
          list.append(item);
        }
        nav.append(list);
      }
      window.addEventListener("hashchange", onHashChange);
      const initial = routeByHash(location.hash) ?? routeById(DEFAULT_ROUTE) ?? ROUTES[0];
      navigate(initial, false);
      await loadMapData();
      selection.subscribe(() => {
        void loadMapData().catch((cause: unknown) => {
          renderRouteError(String(cause));
        });
      });
      if (!location.hash.startsWith("#/")) {
        history.replaceState(null, "", initial.path);
      }
    },
    destroy() {
      window.removeEventListener("hashchange", onHashChange);
      disposePanels();
      mapView.destroy();
    },
  };
}

/* ------------------------------------------------------------------ static bits */

const PROV = {
  phase: "shell-default",
  source_kind: "synthetic",
  authoritative: false,
  simulation_notice: SIMULATION_NOTICE,
} as const;

/** The window the map asks the ledger for: the frozen run's own horizon. */
const WINDOW_FROM = "2026-10-01T09:00:00Z";
const WINDOW_TO = "2026-10-01T10:00:00Z";

export const SHELF_INFO = {
  routes: ROUTES.map((entry) => entry.id),
  hash: shortHash(WORLD.provenance.bundle_manifest_sha256),
};
