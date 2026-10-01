/**
 * Keyboard and focus behaviour.
 *
 * Phase 4 exit criterion: "accessibility checks pass for contrast, focus and keyboard
 * operation". Concretely that means a visible focus ring that never relies on colour
 * alone (2px offset outline plus a 1px dark halo, so it survives any background), one
 * announced destination per route change, focus returned to the control that moved it,
 * and a motion preference that is read live rather than at boot.
 *
 * Simulation only. Not authorised for flight.
 */

let liveRegion: HTMLElement | null = null;

export function ensureLiveRegion(): HTMLElement {
  if (liveRegion && document.body.contains(liveRegion)) {
    return liveRegion;
  }
  const region = document.createElement("div");
  region.id = "console-live";
  region.setAttribute("role", "status");
  region.setAttribute("aria-live", "polite");
  region.setAttribute("aria-atomic", "true");
  region.className = "visually-hidden";
  document.body.append(region);
  liveRegion = region;
  return region;
}

export function announce(message: string): void {
  const region = ensureLiveRegion();
  // Clearing first makes identical repeat messages re-announce.
  region.textContent = "";
  region.textContent = message;
}

const FOCUSABLE_SELECTOR = [
  "a[href]",
  "button:not([disabled])",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  '[tabindex]:not([tabindex="-1"])',
  "summary",
  "details",
].join(", ");

export function focusables(root: ParentNode = document): HTMLElement[] {
  return Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)).filter(
    (element) => element.getClientRects().length > 0,
  );
}

/** Move focus to an element and make sure it is scrolled into view. */
export function focusElement(element: Element | null | undefined, message?: string): boolean {
  if (!(element instanceof HTMLElement)) {
    return false;
  }
  if (!element.hasAttribute("tabindex") && !element.matches(FOCUSABLE_SELECTOR)) {
    element.tabIndex = -1;
  }
  element.focus({ preventScroll: false });
  if (message) {
    announce(message);
  }
  return true;
}

/**
 * Focus containment for the map canvas: MapLibre owns its own tab stops, so the console
 * adds one labelled stop per layer control instead of trapping the keyboard in a canvas.
 */
export function trapWithin(container: HTMLElement, event: KeyboardEvent): void {
  if (event.key !== "Tab") {
    return;
  }
  const items = focusables(container);
  if (items.length === 0) {
    return;
  }
  const first = items[0];
  const last = items[items.length - 1];
  const active = document.activeElement;
  if (event.shiftKey && (active === first || !container.contains(active))) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && active === last) {
    event.preventDefault();
    first.focus();
  }
}

export function prefersReducedMotion(): boolean {
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

export function onReducedMotionChange(listener: (reduced: boolean) => void): () => void {
  const query = window.matchMedia("(prefers-reduced-motion: reduce)");
  const handler = (event: MediaQueryListEvent) => {
    listener(event.matches);
  };
  query.addEventListener("change", handler);
  return () => {
    query.removeEventListener("change", handler);
  };
}

/** Skip link wiring: one keypress from any page to the working region. */
export function installSkipLink(targetId: string): void {
  const link = document.querySelector<HTMLAnchorElement>(`a.skip-link[href="#${targetId}"]`);
  if (!link) {
    return;
  }
  link.addEventListener("click", (event) => {
    const target = document.getElementById(targetId);
    if (target instanceof HTMLElement) {
      event.preventDefault();
      if (!target.hasAttribute("tabindex")) {
        target.tabIndex = -1;
      }
      target.focus();
      target.scrollIntoView({ block: "start" });
      announce(`Skipped to ${target.getAttribute("aria-label") ?? targetId}`);
    }
  });
}

/** Escape always returns to the previous place, never deeper into the app. */
export function onEscape(handler: () => void): () => void {
  const listener = (event: KeyboardEvent) => {
    if (event.key === "Escape") {
      handler();
    }
  };
  document.addEventListener("keydown", listener);
  return () => {
    document.removeEventListener("keydown", listener);
  };
}
