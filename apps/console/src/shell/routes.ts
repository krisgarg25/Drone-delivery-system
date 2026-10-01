/**
 * The console's routes and the panel slots each route exposes.
 *
 * Routes are the seven Phase 4 screens (technical-build/05 section "Phase 4: build").
 * A route owns slots, not widgets: the shell renders the slot containers, the panel
 * registry fills them.
 *
 * Simulation only. Not authorised for flight.
 */

export type RouteId =
  | "map"
  | "missions"
  | "fleet"
  | "economics"
  | "decision"
  | "replay"
  | "comparison";

export type PanelSlot =
  /** Full-width primary surface on the map screen, beside the legend. */
  | "map.context"
  /** Mission queue: the plain-language reason list. */
  | "missions.queue"
  /** Mission board: one mission's card wall. */
  | "missions.board"
  /** Aircraft, pads, chargers, crew. */
  | "fleet.status"
  /** Cost, energy per completion, on-time and unserved rate, baseline comparison. */
  | "economics.metrics"
  /** Inputs, ranking, rejections with human reason plus technical detail. */
  | "decision.trace"
  /** Frozen snapshot, timeline scrubber, replay diff. */
  | "replay.controls"
  /** Two policy runs side by side. */
  | "comparison.runs";

export interface RouteDefinition {
  readonly id: RouteId;
  readonly path: string;
  readonly label: string;
  readonly summary: string;
  readonly slots: readonly PanelSlot[];
  /** The shell renders the map itself; every other route is panel-hosted. */
  readonly ownedBy: "shell" | "panels";
}

export const ROUTES: readonly RouteDefinition[] = [
  {
    id: "map",
    path: "#/map",
    label: "Map",
    summary:
      "Nine visual layers over the frozen Chandigarh synthetic model: basemap, corridor centrelines, occupied reservations, proposed plans, observed telemetry, uncertainty volumes, facility pads, rejected requests and the weather overlay.",
    slots: ["map.context"],
    ownedBy: "shell",
  },
  {
    id: "missions",
    path: "#/missions",
    label: "Missions",
    summary:
      "Queue and board: origin, destination, deadline, aircraft, energy state and the plain-language reason a mission is waiting.",
    slots: ["missions.queue", "missions.board"],
    ownedBy: "panels",
  },
  {
    id: "fleet",
    path: "#/fleet",
    label: "Fleet",
    summary:
      "Aircraft, pads and chargers. Connected is not available; a link with no energy reserve is not either.",
    slots: ["fleet.status"],
    ownedBy: "panels",
  },
  {
    id: "economics",
    path: "#/economics",
    label: "Economics",
    summary:
      "Completed orders, on-time and unserved rate, energy per completion, pad and charger wait, crew time, modelled cost per delivery against the road baseline.",
    slots: ["economics.metrics"],
    ownedBy: "panels",
  },
  {
    id: "decision",
    path: "#/decision",
    label: "Decision trace",
    summary:
      "The stored trace, not a re-derivation: inputs, eligible aircraft, candidate ranking, every rejection with a human reason and the technical detail behind it.",
    slots: ["decision.trace"],
    ownedBy: "panels",
  },
  {
    id: "replay",
    path: "#/replay",
    label: "Replay",
    summary:
      "A replay shows the exact input snapshot the run used, never the current database state, and reports whether the digest still matches.",
    slots: ["replay.controls"],
    ownedBy: "panels",
  },
  {
    id: "comparison",
    path: "#/comparison",
    label: "Comparison",
    summary:
      "Two policy runs, same frozen bundle and seed, metrics side by side including the work that was rejected.",
    slots: ["comparison.runs"],
    ownedBy: "panels",
  },
] as const;

export const SLOT_LABELS: Record<PanelSlot, string> = {
  "map.context": "Map context",
  "missions.queue": "Mission queue",
  "missions.board": "Mission board",
  "fleet.status": "Fleet, pads and chargers",
  "economics.metrics": "Economics",
  "decision.trace": "Decision trace",
  "replay.controls": "Replay controls",
  "comparison.runs": "Scenario comparison",
};

export function routeById(id: string | undefined): RouteDefinition | undefined {
  return ROUTES.find((route) => route.id === id);
}

export function routeByHash(hash: string): RouteDefinition | undefined {
  const clean = hash.replace(/^#\/?/, "");
  return ROUTES.find((route) => route.id === clean);
}

export const DEFAULT_ROUTE: RouteId = "map";
