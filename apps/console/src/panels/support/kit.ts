/**
 * Panel support kit: the presentation primitives every operations panel shares.
 *
 * It exists so a panel never invents its own loading spinner, error block or focus
 * behaviour. Four rules are enforced here rather than repeated in each panel:
 *
 *  1. every payload arrives through the typed client and is drawn with its own
 *     `source_kind` / `authoritative` / transport mode attached (docs/conventions.md §3);
 *  2. every request has a real designed loading, empty, error and boundary variant - a
 *     spinner is never an acceptable state (technical-build/05, Phase 4 build list);
 *  3. contract strings go into `textContent`, never into markup, so a reason code from
 *     the API cannot become an element;
 *  4. a drawer traps focus while it is open and returns it to the control that opened it.
 *
 * Nothing in this module decides, ranks or validates a mission. It renders what the
 * control plane already decided.
 *
 * Simulation only. Not authorised for flight.
 */

import "./kit.css";
import { focusables, onEscape, trapWithin } from "../../lib/a11y";
import type { ApiError, ApiResult, ResponseMeta } from "../../lib/api/client";
import type { DataState } from "../../lib/api/types";
import { ageOf, formatDate, shortHash } from "../../lib/provenance";
import type { PanelContext } from "../../shell/panels";

/* ------------------------------------------------------------------ operator hand-off */

/**
 * Which panel the operator is on their way to, and why.
 *
 * The queue's rejection button and the trace panel must agree on one thing: the operator
 * is going to a specific mission's refusal and expects to land on the answer, not at the
 * top of a new screen. `context.selection` carries the mission; this carries the intent,
 * so the destination panel knows to move focus and announce. It is view state only - no
 * panel reads it for anything about the mission itself.
 */
export type HandoffKind = "decision-trace" | "replay";

let handoff: { readonly kind: HandoffKind; readonly missionId: string } | null = null;

export function setHandoff(kind: HandoffKind, missionId: string): void {
  handoff = { kind, missionId };
}

/** Consumes the intent: the first reader gets it, later readers do not. */
export function takeHandoff(kind: HandoffKind, missionId: string | null): boolean {
  if (handoff === null) {
    return false;
  }
  if (handoff.kind !== kind || handoff.missionId !== missionId) {
    return false;
  }
  handoff = null;
  return true;
}

/* ------------------------------------------------------------------ element builder */

interface ElInit {
  class?: string;
  text?: string;
  id?: string;
  title?: string;
  hidden?: boolean;
  /** `type` on buttons and inputs; every other tag ignores it. */
  type?: string;
  /** `for` on a label. */
  for?: string;
  role?: string;
  /** `scope` on a table cell. */
  scope?: string;
  aria?: Record<string, string>;
  data?: Record<string, string>;
}

export function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  init: ElInit = {},
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (init.class !== undefined) {
    node.className = init.class;
  }
  if (init.text !== undefined) {
    node.textContent = init.text;
  }
  if (init.id !== undefined) {
    node.id = init.id;
  }
  if (init.title !== undefined) {
    node.title = init.title;
  }
  if (init.hidden === true) {
    node.hidden = true;
  }
  if (init.type !== undefined) {
    node.setAttribute("type", init.type);
  }
  if (init.role !== undefined) {
    node.setAttribute("role", init.role);
  }
  if (init.for !== undefined) {
    node.setAttribute("for", init.for);
  }
  if (init.scope !== undefined) {
    node.setAttribute("scope", init.scope);
  }
  for (const [name, value] of Object.entries(init.aria ?? {})) {
    node.setAttribute(`aria-${name}`, value);
  }
  for (const [name, value] of Object.entries(init.data ?? {})) {
    node.dataset[name] = value;
  }
  return node;
}

export function block<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  init: ElInit,
  children: readonly Node[],
): HTMLElementTagNameMap[K] {
  const node = el(tag, init);
  node.append(...children);
  return node;
}

/**
 * A label/value pair. It is a `<dl>` in its own right, which is what makes it safe anywhere:
 * in a field list, in a pending block, inside an error state. A `dt`/`dd` pair is only valid
 * inside a `dl`, and this primitive is used by panels that never wrap it in one - a row that
 * carries its own list element cannot produce an orphan pair (axe `dlitem`, the serious
 * violation in docs/phase-4-console-audit.md F-3). Fixing it here means no panel patches it.
 */
export function field(label: string, value: string, hint?: string): HTMLElement {
  const dt = el("dt", { text: label, class: "kit-field-label" });
  const dd = el("dd", { class: "kit-field-value" });
  const code = el("span", { text: value, class: "kit-field-text" });
  if (hint !== undefined) {
    code.title = hint;
  }
  dd.append(code);
  return block("dl", { class: "kit-field" }, [dt, dd]);
}

/**
 * The dense row grid the panels are made of. Each row is already a `dl`, so the container is
 * a named group rather than a second list: nesting a `dl` inside a `dl` would be invalid
 * markup, and an unnamed group would leave the label the rows are read against unsaid.
 */
export function fieldList(rows: readonly Node[], label: string): HTMLElement {
  return block("div", { class: "kit-fields", role: "group", aria: { label } }, rows);
}

/** A short id a human can read aloud: `3333…3333`, with the full value in the title. */
export function shortId(value: string | undefined, keep = 8): string {
  if (value === undefined || value === "") {
    return "not stated by the API";
  }
  const clean = value.replace(/-/g, "");
  if (clean.length <= keep * 2) {
    return value;
  }
  return `${clean.slice(0, keep)}…${clean.slice(-keep)}`;
}

export function mono(value: string, title?: string): HTMLElement {
  const node = el("span", { text: value, class: "kit-mono" });
  if (title !== undefined) {
    node.title = title;
  }
  return node;
}

/** Microseconds as the dispatcher reports them: seconds with three decimals, or ms. */
export function formatMicros(us: number): string {
  const seconds = us / 1_000_000;
  if (Math.abs(seconds) < 1) {
    return `${Math.round(us / 1000)} ms`;
  }
  return `${seconds.toFixed(3)} s`;
}

export function formatRatio(value: number, digits = 1): string {
  return `${(value * 100).toFixed(digits)}%`;
}

export function formatNumber(value: number, digits = 0): string {
  return value.toLocaleString("en", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

/* ------------------------------------------------------------------ designed states */

export function loadingState(subject: string, detail: string): HTMLElement {
  const bar = el("div", { class: "kit-skeleton", aria: { hidden: "true" } });
  const note = el("p", {
    class: "kit-state kit-state--loading",
    role: "status",
    text: `Reading ${subject} from the control plane. ${detail}`,
  });
  const wrap = el("div", { class: "kit-state-wrap" });
  wrap.append(note, bar, bar.cloneNode(true) as HTMLElement, bar.cloneNode(true) as HTMLElement);
  return wrap;
}

/** A real empty state: it names the query that produced nothing, and what would fill it. */
export function emptyState(subject: string, why: string, whatWouldFillIt: string): HTMLElement {
  const wrap = el("div", { class: "kit-state-wrap" });
  const heading = el("h4", { class: "kit-state-heading", text: `Nothing in ${subject}` });
  const body = el("p", { class: "kit-state kit-state--empty", text: why });
  const hint = el("p", {
    class: "kit-state-hint",
    text: `What would appear here: ${whatWouldFillIt}`,
  });
  wrap.append(heading, body, hint);
  return wrap;
}

/**
 * A boundary state is not empty: the data exists but is too thin, too stale or too
 * partial to carry the claim being made from it. The panel must say so rather than
 * drawing a confident chart over one sample.
 */
export function boundaryState(title: string, detail: string, consequence: string): HTMLElement {
  const wrap = el("div", { class: "kit-state-wrap kit-state-wrap--boundary" });
  wrap.append(
    el("h4", { class: "kit-state-heading", text: title }),
    el("p", { class: "kit-state kit-state--boundary", text: detail }),
    el("p", { class: "kit-state-hint", text: `So the figure below is: ${consequence}` }),
  );
  return wrap;
}

/** The error state names the failing operation and offers the one action the console has. */
export function errorState(error: ApiError, retry: () => void): HTMLElement {
  const wrap = el("div", { class: "kit-state-wrap kit-state-wrap--error" });
  wrap.setAttribute("role", "alert");
  wrap.append(
    el("h4", { class: "kit-state-heading", text: `${error.operation} did not answer` }),
    el("p", { class: "kit-state kit-state--error", text: `${error.kind}: ${error.message}` }),
  );
  if (error.detail !== undefined) {
    wrap.append(field("request", error.detail));
  }
  wrap.append(
    el("p", {
      class: "kit-state-hint",
      text: "Nothing was inferred to fill the gap. No scheduling runs in this browser, so a panel with no data shows no data.",
    }),
    el("button", {
      class: "kit-button",
      text: "Read it again",
      type: "button",
      aria: { label: `Read ${error.operation} again` },
    }),
  );
  const button = wrap.querySelector("button");
  if (button instanceof HTMLButtonElement) {
    button.addEventListener("click", retry);
  }
  return wrap;
}

/* ------------------------------------------------------------------ provenance footer */

/**
 * The strip every rendered block carries: which transport answered, which operation
 * produced it, when, and what the payload itself claims to be. A synthetic answer can
 * never be mistaken for a measurement because of this line, not because of a colour.
 */
export function provenanceLine(context: PanelContext, meta: ResponseMeta): HTMLElement {
  const state = context.stateChip(
    meta.provenance?.source_kind ??
      (context.api.transport.mode === "live" ? "observed" : "synthetic"),
    meta.provenance?.source_kind ?? "unlabelled",
  );
  const parts = [
    mono(meta.transportMode === "live" ? "live /v1" : "synthetic fixtures"),
    document.createTextNode(` · ${meta.operation} · `),
    mono(ageOf(meta.receivedAt), meta.receivedAt),
    document.createTextNode(" · authoritative: "),
    mono(String(meta.provenance?.authoritative ?? false)),
  ];
  const wrap = block("p", { class: "kit-prov", aria: { label: "data provenance" } }, [
    state,
    ...parts,
  ]);
  return wrap;
}

/* ------------------------------------------------------------------ resource loading */

export interface Read<T> {
  readonly data: T;
  readonly meta: ResponseMeta;
}

export interface LoadOptions<T> {
  readonly subject: string;
  readonly detail?: string;
  /** True when the payload is present but carries no rows: the empty variant is drawn. */
  readonly isEmpty?: (data: T) => boolean;
  readonly empty?: (data: T) => HTMLElement;
  /** Drawn after the payload, before the panel body: the boundary check gets to veto. */
  readonly note?: (data: T) => HTMLElement | null;
}

export interface Handle {
  refresh(): void;
  dispose(): void;
}

/**
 * One request, four states. The panel supplies `render`; the kit supplies loading, error,
 * empty and the provenance line, so no panel can forget one of them.
 */
export function load<T>(
  target: HTMLElement,
  context: PanelContext,
  run: () => Promise<ApiResult<T>>,
  render: (read: Read<T>) => readonly HTMLElement[],
  options: LoadOptions<T>,
): Handle {
  let disposed = false;
  let alive = true;

  const place = (node: HTMLElement): void => {
    if (!disposed && document.body.contains(target)) {
      target.replaceChildren(node);
    }
  };

  const go = (): void => {
    if (disposed) {
      return;
    }
    place(loadingState(options.subject, options.detail ?? "the request is in flight."));
    void run().then(
      (result) => {
        if (disposed || !alive || !document.body.contains(target)) {
          return;
        }
        if (!result.ok) {
          place(errorState(result.error, go));
          context.announce(`${options.subject}: ${result.error.message}`);
          return;
        }
        const body = render({ data: result.data, meta: result.meta });
        const wrap = el("div", { class: "kit-resource" });
        const note = options.note?.(result.data);
        if (note !== null && note !== undefined) {
          wrap.append(note);
        }
        if (options.isEmpty?.(result.data) === true) {
          wrap.append(
            options.empty?.(result.data) ??
              emptyState(
                options.subject,
                "The ledger answered with no rows.",
                "a committed mission in this window.",
              ),
          );
        } else {
          wrap.append(...body);
        }
        wrap.append(provenanceLine(context, result.meta));
        place(wrap);
      },
      (cause: unknown) => {
        if (disposed) {
          return;
        }
        place(
          emptyState(
            options.subject,
            `The client threw instead of returning a result: ${String(cause)}. A thrown client is a fault, not an empty ledger, so it is reported separately.`,
            "the API answering this operation at all - see the provenance strip for which transport is live.",
          ),
        );
      },
    );
  };

  return {
    refresh: go,
    dispose() {
      disposed = true;
      alive = false;
    },
  };
}

/** Several requests, one loading/error pass, and a named error per failed slot. */
export function pendingRows(jobs: readonly { subject: string; operation: string }[]): HTMLElement {
  const wrap = el("div", { class: "kit-partial" });
  for (const job of jobs) {
    wrap.append(
      field(
        job.subject,
        `pending - ${job.operation}`,
        "this part of the panel has not answered yet",
      ),
    );
  }
  return wrap;
}

/* ------------------------------------------------------------------ detail drawer */

export interface DrawerField {
  readonly label: string;
  readonly value: string;
  readonly hint?: string;
}

export interface DrawerSpec {
  readonly title: string;
  readonly subtitle?: string;
  readonly state?: DataState;
  readonly fields: readonly DrawerField[];
  readonly paragraphs?: readonly string[];
}

/**
 * A focus-trapped detail drawer. It opens where the operator already is (inside the
 * panel, so nothing is rendered outside the shell's banner), moves focus to its own
 * heading, cycles Tab inside itself, and on close returns focus to the trigger - which is
 * the part a drawer usually gets wrong.
 */
export function openDrawer(
  context: PanelContext,
  host: HTMLElement,
  spec: DrawerSpec,
  trigger?: HTMLElement,
): void {
  const previous = document.activeElement instanceof HTMLElement ? document.activeElement : trigger;
  const overlay = el("div", { class: "kit-drawer-overlay" });
  const drawer = el("div", {
    class: "kit-drawer",
    role: "dialog",
    aria: { modal: "true", labelledby: "kit-drawer-title" },
  });
  const title = el("h4", { id: "kit-drawer-title", class: "kit-drawer-title", text: spec.title });
  drawer.append(title);
  if (spec.subtitle !== undefined) {
    drawer.append(el("p", { class: "kit-drawer-sub", text: spec.subtitle }));
  }
  if (spec.state !== undefined) {
    drawer.append(context.stateChip(spec.state));
  }
  const rows = spec.fields.map((entry) =>
    field(entry.label, entry.value, entry.hint ?? `the API reported: ${entry.value}`),
  );
  drawer.append(fieldList(rows, "technical detail"));
  for (const text of spec.paragraphs ?? []) {
    drawer.append(el("p", { class: "kit-drawer-body", text }));
  }
  const close = el("button", {
    class: "kit-button kit-button--primary",
    text: "Close detail (Escape)",
    type: "button",
  });
  drawer.append(close);
  overlay.append(drawer);
  host.append(overlay);

  let released = false;
  const release = (): void => {
    if (released) {
      return;
    }
    released = true;
    document.removeEventListener("keydown", onKey, true);
    stopListening();
    overlay.remove();
    if (previous instanceof HTMLElement && document.body.contains(previous)) {
      previous.focus();
    } else if (trigger !== undefined && document.body.contains(trigger)) {
      trigger.focus();
    }
  };
  const onKey = (event: KeyboardEvent): void => {
    if (event.key === "Tab") {
      trapWithin(drawer, event);
    }
  };
  document.addEventListener("keydown", onKey, true);
  const stopListening = onEscape(release);
  close.addEventListener("click", release);
  overlay.addEventListener("click", (event) => {
    if (event.target === overlay) {
      release();
    }
  });
  title.tabIndex = -1;
  title.focus();
  context.announce(`${spec.title} opened. Press Escape to close and return where you were.`);
}

/** The button that opens a drawer, wired once so every panel's drawer behaves the same. */
export function drawerButton(
  context: PanelContext,
  host: HTMLElement,
  spec: DrawerSpec,
  label: string,
): HTMLElement {
  const button = el("button", {
    class: "kit-button",
    type: "button",
    text: label,
    aria: { label: `${label}. Opens a detail drawer with the technical evidence.` },
  });
  button.addEventListener("click", () => {
    openDrawer(context, host, spec, button);
  });
  return button;
}

/* ------------------------------------------------------------------ tables */

export interface Column<Row> {
  readonly header: string;
  readonly cell: (row: Row) => string;
  /** Optional long form shown in the `title` attribute - the definition on hover. */
  readonly hint?: (row: Row) => string;
}

export function table<Row>(
  rows: readonly Row[],
  columns: readonly Column<Row>[],
  label: string,
  rowAttributes?: (row: Row, index: number) => ElInit,
): HTMLElement {
  const thead = el("thead");
  const headRow = el("tr");
  for (const column of columns) {
    headRow.append(el("th", { text: column.header, scope: "col" }));
  }
  thead.append(headRow);
  const tbody = el("tbody");
  rows.forEach((row, index) => {
    const tr = el("tr", rowAttributes?.(row, index) ?? {});
    columns.forEach((column, position) => {
      const text = column.cell(row);
      const cell = position === 0 ? el("th", { scope: "row" }) : el("td");
      const content = el("span", { text });
      const hint = column.hint?.(row);
      if (hint !== undefined) {
        content.title = hint;
      }
      cell.append(content);
      tr.append(cell);
    });
    tbody.append(tr);
  });
  return block("table", { class: "kit-table", aria: { label } }, [thead, tbody]);
}

/* ------------------------------------------------------------------ misc */

export function sectionTitle(text: string, id?: string): HTMLElement {
  const node = el("h4", { class: "kit-section-title", text });
  if (id !== undefined) {
    node.id = id;
  }
  return node;
}

export function paragraph(text: string, className = "kit-paragraph"): HTMLElement {
  return el("p", { class: className, text });
}

export function hashLine(label: string, value: string): HTMLElement {
  return field(label, shortHash(value), value);
}

/** Facility ids are synthetic node ids; the frozen bundle supplies the fictional name. */
export function facilityName(
  id: string | undefined,
  pads: readonly { facility_id: string; name: string }[],
): string {
  if (id === undefined || id === "") {
    return "not stated by the API";
  }
  const hit = pads.find((pad) => pad.facility_id === id || id.startsWith(pad.facility_id));
  return hit === undefined ? id : `${hit.name} (${id})`;
}

export function formatWhen(iso: string | undefined): string {
  return iso === undefined || iso === "" ? "not stated by the API" : formatDate(iso);
}

/** One control, keyboard-labelled, that never mutates anything. */
export function button(
  text: string,
  onClick: () => void,
  options: { ariaLabel?: string; primary?: boolean; disabled?: boolean } = {},
): HTMLElement {
  const node = el("button", {
    class: `kit-button${options.primary === true ? " kit-button--primary" : ""}`,
    type: "button",
    text,
    aria: { label: options.ariaLabel ?? text },
  });
  if (options.disabled === true) {
    node.disabled = true;
  }
  node.addEventListener("click", onClick);
  return node;
}

/**
 * Move focus to a panel heading and announce it. Used by the rejection path so the
 * operator lands on the answer instead of at the top of a new page.
 */
export function focusAndAnnounce(context: PanelContext, node: HTMLElement, message: string): void {
  if (!node.hasAttribute("tabindex")) {
    node.tabIndex = -1;
  }
  node.scrollIntoView({ block: "nearest", behavior: context.reducedMotion() ? "auto" : "smooth" });
  node.focus();
  context.announce(message);
}

export { focusables };
