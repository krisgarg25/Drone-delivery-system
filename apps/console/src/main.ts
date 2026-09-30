/**
 * Phase 0 console shell.
 *
 * It renders what the API says and nothing else. No mission, reservation, dispatch or
 * separation decision may be computed here (docs/conventions.md section 7).
 */

interface HealthResponse {
  status: string;
  phase: string;
  authorized_for_flight: boolean;
  detail: string;
}

interface VersionResponse {
  version: string;
  api_version: string;
  domain_schema_version: number;
}

const BANNER_TEXT = "Simulation only. Not authorised for flight.";

function renderDefinitions(target: HTMLElement, entries: [string, string][]): void {
  target.replaceChildren();
  for (const [term, value] of entries) {
    const dt = document.createElement("dt");
    dt.textContent = term;
    const dd = document.createElement("dd");
    // textContent, never innerHTML: API strings stay data and cannot become markup.
    dd.textContent = value;
    target.append(dt, dd);
  }
}

async function readJson<T>(path: string): Promise<T | null> {
  try {
    const response = await fetch(path, { headers: { accept: "application/json" } });
    if (!response.ok) {
      return null;
    }
    return (await response.json()) as T;
  } catch {
    return null;
  }
}

async function render(): Promise<void> {
  const list = document.getElementById("status-list");
  const health = await readJson<HealthResponse>("/health");
  const version = await readJson<VersionResponse>("/version");

  if (!(list instanceof HTMLElement)) {
    return;
  }

  if (!health) {
    renderDefinitions(list, [
      ["api", "unreachable - start it with `make api`"],
      ["mode", "simulation"],
    ]);
    return;
  }

  renderDefinitions(list, [
    ["api status", health.status],
    ["phase", health.phase],
    ["authorised for flight", health.authorized_for_flight ? "yes" : "no"],
    ["build", version ? `${version.version} (${version.api_version})` : "unknown"],
    ["detail", health.detail],
  ]);
}

function assertBanner(): void {
  const banner = document.querySelector<HTMLElement>(".safety-banner");
  const text = banner === null ? "" : banner.textContent;
  if (!text.includes(BANNER_TEXT)) {
    throw new Error("the simulation-only banner is mandatory on every screen");
  }
}

assertBanner();
void render();
