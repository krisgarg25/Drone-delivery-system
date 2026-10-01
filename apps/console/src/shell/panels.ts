/**
 * THE PANEL SEAM. Panels are not allowed to touch the shell; they register here.
 *
 * To add a panel: create `src/panels/<name>.ts` exporting a `Panel`, then add it to
 * `PANELS` below (or call `registerPanel` from `src/main.ts` before `start()`). The shell
 * mounts it into its `slot` container on the route that declares that slot - no shell or
 * router edit, and no other panel can be disturbed by it.
 *
 * Everything a panel needs is on `PanelContext`: the typed client, the map layer and
 * selection stores, the live region and the motion preference. A panel must render
 * `source_kind` / `authoritative` the way it receives them and must never compute a
 * reservation, dispatch or command (docs/conventions.md section 7; the ESLint
 * no-restricted-syntax rule blocks `reserve|commit|dispatch|launch|command` calls).
 *
 * Simulation only. Not authorised for flight.
 */

import type { Api } from "../lib/api/endpoints";
import type { DataState } from "../lib/api/types";
import type { LayerId } from "../map/layers";
import type { PanelSlot } from "./routes";

/**
 * A panel that needs teardown (an EventSource, a ResizeObserver, a subscription) registers
 * a disposer through `context.onDispose` rather than returning one, so `mount` has exactly
 * one contract: render into the host.
 */
export type Disposer = () => void;

export interface PanelContext {
  /** Typed read client over the contract. `transport.mode` tells you if it is synthetic. */
  readonly api: Api;
  /** Layer visibility and the legend's second channels. Only the map route has a canvas. */
  readonly layers: LayerController;
  /** Shared selection: which mission / aircraft / corridor the operator is looking at. */
  readonly selection: SelectionStore;
  /** Screen-reader announcement. Use it for state changes a sighted user gets visually. */
  announce(message: string): void;
  /** True when the operator asked for reduced motion. Never animate a data state. */
  reducedMotion(): boolean;
  /** Render one semantic state into a labelled, non-colour-only chip. */
  stateChip(state: DataState, text?: string): HTMLElement;
  /** Called by the shell when the route changes. Register every subscription here. */
  onDispose(disposer: Disposer): void;
}

export interface Panel {
  readonly id: string;
  readonly slot: PanelSlot;
  readonly title: string;
  readonly description: string;
  /**
   * Render into `host`. The shell clears the host first and sets `aria-labelledby` to the
   * slot heading, so a panel never writes its own `<h2>`; start at `<h3>`.
   */
  mount(host: HTMLElement, context: PanelContext): void;
}

/* ------------------------------------------------------------------ stores */

export interface Selection {
  readonly missionId: string | null;
  readonly aircraftId: string | null;
  readonly corridorId: string | null;
  readonly scenarioId: string;
}

export interface SelectionStore {
  get(): Selection;
  patch(next: Partial<Selection>): void;
  subscribe(listener: (selection: Selection) => void): () => void;
}

export interface LayerController {
  visible(id: LayerId): boolean;
  setVisible(id: LayerId, visible: boolean): void;
  toggle(id: LayerId): void;
  all(): readonly LayerId[];
  subscribe(listener: (visible: ReadonlySet<LayerId>) => void): () => void;
}

export function createSelectionStore(initial: Selection): SelectionStore {
  let current: Selection = initial;
  const listeners = new Set<(selection: Selection) => void>();
  return {
    get: () => current,
    patch(next) {
      current = { ...current, ...next };
      for (const listener of listeners) {
        listener(current);
      }
    },
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
  };
}

export function createLayerController(
  ids: readonly LayerId[],
  initiallyVisible: readonly LayerId[] = ids,
): LayerController & { snapshot(): ReadonlySet<LayerId> } {
  const visible = new Set<LayerId>(initiallyVisible);
  const listeners = new Set<(visible: ReadonlySet<LayerId>) => void>();
  const emit = () => {
    for (const listener of listeners) {
      listener(visible);
    }
  };
  return {
    visible: (id) => visible.has(id),
    setVisible(id, next) {
      if (visible.has(id) === next) {
        return;
      }
      if (next) {
        visible.add(id);
      } else {
        visible.delete(id);
      }
      emit();
    },
    toggle(id) {
      this.setVisible(id, !visible.has(id));
    },
    all: () => ids,
    snapshot: () => new Set(visible),
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
  };
}

/* ------------------------------------------------------------------ registry */

export const PANELS: Panel[] = [
  // Empty on purpose. The map, the shell, the design system and the typed client are
  // Phase 4's foundation; the queue, board, fleet, economics, decision, replay and
  // comparison panels are mounted through this array by the panel engineer.
];

const registered = new Map<string, Panel>();

export function registerPanel(panel: Panel): () => void {
  if (registered.has(panel.id)) {
    throw new Error(`panel id "${panel.id}" is already registered`);
  }
  registered.set(panel.id, panel);
  PANELS.push(panel);
  return () => {
    registered.delete(panel.id);
    const index = PANELS.indexOf(panel);
    if (index >= 0) {
      PANELS.splice(index, 1);
    }
  };
}

export function panelsForSlot(slot: PanelSlot): readonly Panel[] {
  return PANELS.filter((panel) => panel.slot === slot);
}

/** A human-readable list of what is mounted, for the shell's dev self-check. */
export function registeredPanelIds(): string[] {
  return PANELS.map((panel) => `${panel.slot}/${panel.id}`);
}
