/**
 * Phase 4 console bootstrap.
 *
 * Transport choice is the one decision made at startup, and it is made visibly rather
 * than silently: probe `/health`, use the live control plane if it answers, fall back to
 * the synthetic dev transport if it does not, and let `?transport=live|synthetic` force
 * either. Whichever wins, the shell prints it in the provenance strip, and a synthetic
 * payload is drawn with the synthetic second channels, so a fallback can never be
 * mistaken for a measurement.
 *
 * Simulation only. Not authorised for flight.
 */

import { announce } from "./lib/a11y";
import { createHttpTransport, SIMULATION_NOTICE } from "./lib/api/client";
import { createSyntheticTransport } from "./lib/api/mock";
import { createApi } from "./lib/api/endpoints";
import type { Transport } from "./lib/api/client";
import { createShell } from "./shell/shell";

function requestedTransport(): string | null {
  return new URLSearchParams(location.search).get("transport");
}

async function pickTransport(): Promise<{ transport: Transport; note: string }> {
  const forced = requestedTransport();
  if (forced === "synthetic" && !import.meta.env.PROD) {
    return { transport: createSyntheticTransport(), note: "forced by ?transport=synthetic" };
  }
  if (forced === "live") {
    return { transport: createHttpTransport(), note: "forced by ?transport=live" };
  }
  const health = await probe("/health");
  if (health !== null && !health.authorized_for_flight) {
    return {
      transport: createHttpTransport(),
      note: `API answered /health (phase ${health.phase})`,
    };
  }
  if (import.meta.env.PROD) {
    // A production build has no fixture transport at all; it either reaches the API or
    // says it cannot.
    return {
      transport: createHttpTransport(),
      note: "API did not answer; no fixtures exist in a production build",
    };
  }
  return {
    transport: createSyntheticTransport(),
    note: "API did not answer /health - falling back to the synthetic dev transport",
  };
}

interface HealthPayload {
  status: string;
  phase: string;
  authorized_for_flight: boolean;
}

async function probe(path: string): Promise<HealthPayload | null> {
  try {
    const controller = new AbortController();
    const timer = setTimeout(() => {
      controller.abort();
    }, 400);
    const response = await fetch(path, { signal: controller.signal });
    clearTimeout(timer);
    if (!response.ok) {
      return null;
    }
    return (await response.json()) as HealthPayload;
  } catch {
    return null;
  }
}

/** The banner is mandatory on every screen, error screens included. Verified, not assumed. */
function assertBanner(): void {
  const found = Array.from(document.querySelectorAll<HTMLElement>(".safety-banner"));
  const ok =
    found.length >= 1 && found.every((node) => node.textContent.includes(SIMULATION_NOTICE));
  if (!ok) {
    throw new Error("the simulation-only banner is mandatory on every screen");
  }
}

function renderFatal(message: string): void {
  const main = document.getElementById("main");
  if (!(main instanceof HTMLElement)) {
    return;
  }
  main.replaceChildren();
  const heading = document.createElement("h2");
  heading.id = "route-heading";
  heading.tabIndex = -1;
  heading.textContent = "The console stopped";
  const detail = document.createElement("p");
  detail.setAttribute("role", "alert");
  detail.textContent = `${message}. The banner above still applies: nothing on this screen authorises a flight, and no scheduling logic runs in this browser.`;
  main.append(heading, detail);
  heading.focus();
}

async function boot(): Promise<void> {
  assertBanner();
  const { transport, note } = await pickTransport();
  const api = createApi(transport);
  const shell = createShell(api);
  await shell.start();
  announce(`Console ready. Transport: ${transport.label}. ${note}`);
}

window.addEventListener("error", (event) => {
  renderFatal(`An unhandled error occurred: ${event.message}`);
});

window.addEventListener("unhandledrejection", (event) => {
  renderFatal(`A request failed unexpectedly: ${String(event.reason)}`);
});

void boot().catch((cause: unknown) => {
  renderFatal(`Startup failed: ${String(cause)}`);
});
