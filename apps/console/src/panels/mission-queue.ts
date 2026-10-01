/**
 * Mission queue - the operations screen an operator actually reads.
 *
 * docs/05-chandigarh-demo.md 5.4: customer, origin, destination, deadline, assigned
 * aircraft, energy state and a plain-language reason. Refused and unserved work is drawn
 * FIRST and is never filtered away: the queue is where a rejection is found, and Phase 4's
 * acceptance criterion is that its cause is readable in under 60 seconds, so the queue's
 * job beyond listing orders is to hand the operator straight into the trace.
 *
 * The origin, destination and deadline are not on an order in this contract
 * (`OrderResponse` carries identity, status and priority only). They are read from the
 * decision the server stored for that mission - `DecisionTraceResponse.inputs` - which is
 * also where the reason code lives. A field the API did not send prints as
 * "not stated by the API"; the console never fills a gap with an invention.
 *
 * Simulation only. Not authorised for flight.
 */

import type { Api } from "../lib/api/endpoints";
import type { ApiResult, ResponseMeta } from "../lib/api/client";
import type { Schemas } from "../lib/api/types";
import { padFor } from "../map/world";
import type { Panel, PanelContext } from "../shell/panels";
import {
  block,
  el,
  emptyState,
  errorState,
  formatWhen,
  loadingState,
  mono,
  paragraph,
  provenanceLine,
  sectionTitle,
  setHandoff,
  shortId,
} from "./support/kit";
import { reasonOfMission } from "./support/reasons";
import type { ExplainedReason } from "./support/reasons";
import "./mission-queue.css";

/** A queue row: one order, joined to whatever the decision trace says about it. */
interface QueueRow {
  readonly order: Schemas["OrderResponse"];
  readonly trace: Schemas["DecisionTraceResponse"] | null;
  readonly plans: readonly Schemas["PlanVersionResponse"][];
  readonly traceFailure: string | null;
}

interface Gathered {
  readonly rows: readonly QueueRow[];
  readonly failures: readonly string[];
  readonly meta: ResponseMeta | null;
  readonly ordersFailed: boolean;
  readonly skipped: number;
}

/**
 * Cap on per-mission fan-out: each row costs two reads, so an unbounded queue would cost
 * 2N requests. The cap is stated on screen rather than hidden.
 */
const MAX_ROWS = 24;

function plannedFor(row: QueueRow): Schemas["PlanVersionResponse"] | null {
  const ranked = row.plans.find((plan) => plan.rank === 1);
  if (ranked !== undefined) {
    return ranked;
  }
  return row.plans.at(0) ?? null;
}

function aircraftFor(row: QueueRow): string {
  const assigned = plannedFor(row)?.aircraft_id;
  if (assigned !== undefined) {
    return assigned;
  }
  const eligible = row.trace?.eligible?.[0];
  if (eligible !== undefined) {
    return eligible.aircraft_id;
  }
  return "none assigned by the ledger";
}

interface EnergyReading {
  readonly value: string;
  readonly hint: string;
}

function energyFor(row: QueueRow): EnergyReading {
  const plan = plannedFor(row);
  const eligible = row.trace?.eligible?.[0];
  const margin = plan?.energy_margin_wh ?? eligible?.energy_margin_wh;
  const recovery = eligible?.recovery_margin_wh;
  if (margin === undefined) {
    return {
      value: "no energy figure on record",
      hint: "neither a plan version nor an eligible-aircraft row carried energy_margin_wh",
    };
  }
  const source =
    plan !== null ? `plan version rank ${plan.rank ?? "n/a"}` : "eligible aircraft row";
  return {
    value: `${margin} Wh margin${recovery === undefined ? "" : `, ${recovery} Wh recovery`}`,
    hint: `energy_margin_wh from the ${source}. The console shows the number and does not judge it: whether a margin clears the reserve is the ledger's decision, printed as a reason code.`,
  };
}

function reasonFor(row: QueueRow): ExplainedReason {
  return reasonOfMission(row.trace);
}

/** Refused, held or unserved. An accepted commitment is not blocked, whatever its status word. */
function isBlocked(row: QueueRow): boolean {
  if (row.trace === null) {
    return true;
  }
  const outcome = row.trace.record.outcome ?? "unknown";
  if (outcome === "rejected") {
    return true;
  }
  const status = row.order.status.toLowerCase();
  return /reject|unserved|failed|cancel|waiting|held|blocked|unresolved|draft/.test(status);
}

function okData<T>(result: ApiResult<T>): T | null {
  return result.ok ? result.data : null;
}

async function gather(api: Api): Promise<Gathered> {
  const orders = await api.orders();
  if (!orders.ok) {
    return {
      rows: [],
      failures: [`${orders.error.operation}: ${orders.error.message}`],
      meta: null,
      ordersFailed: true,
      skipped: 0,
    };
  }
  const items = orders.data.items;
  const head = items.slice(0, MAX_ROWS);
  const rows: QueueRow[] = [];
  const failures: string[] = [];
  for (const order of head) {
    const [trace, plans] = await Promise.all([
      api.decisionTrace(order.mission_id),
      api.planVersions(order.mission_id),
    ]);
    if (!trace.ok) {
      failures.push(
        `${shortId(order.mission_id)}: ${trace.error.operation} - ${trace.error.message}`,
      );
    }
    rows.push({
      order,
      trace: okData(trace),
      plans: plans.ok ? plans.data.items : [],
      traceFailure: trace.ok ? null : `${trace.error.operation}: ${trace.error.message}`,
    });
  }
  return {
    rows,
    failures,
    meta: orders.meta,
    ordersFailed: false,
    skipped: items.length - head.length,
  };
}

/* ------------------------------------------------------------------ row rendering */

function cell(children: readonly HTMLElement[], header = false): HTMLElement {
  const node = header ? el("th", { scope: "row" }) : el("td");
  node.append(...children);
  return node;
}

function sub(text: string): HTMLElement {
  return el("span", { class: "mq-sub", text });
}

function padName(id: string | undefined): string {
  if (id === undefined || id === "") {
    return "not stated by the API";
  }
  const pad = padFor(id);
  return pad === undefined ? id : `${pad.name} - ${id}`;
}

function reasonCell(row: QueueRow, context: PanelContext): HTMLElement {
  const reason = reasonFor(row);
  const refused = isBlocked(row);
  const cell = el("div", { class: "mq-reason", title: reason.hint });
  cell.append(
    el("span", { class: "mq-reason-plain", text: reason.bucket.plain }),
    el("span", {
      class: "mq-reason-sentence",
      text: reason.humanReason ?? "the trace carried no sentence for this mission",
    }),
    el("span", { class: "mq-reason-code", text: `wire code: ${reason.code}` }),
    el("button", {
      class: "mq-open-trace",
      type: "button",
      text: refused ? "Why? Open the decision trace" : "Open its decision trace",
      aria: {
        label: refused
          ? `Why was mission ${shortId(row.order.mission_id)} refused? Opens the decision trace screen with the refusal focused.`
          : `Decision trace for mission ${shortId(row.order.mission_id)}.`,
      },
    }),
  );
  const trigger = cell.querySelector("button");
  if (trigger instanceof HTMLButtonElement) {
    trigger.addEventListener("click", () => {
      context.selection.patch({ missionId: row.order.mission_id });
      setHandoff("decision-trace", row.order.mission_id);
      context.announce(
        `Opening the decision trace for mission ${shortId(row.order.mission_id)}: ${reason.bucket.plain}.`,
      );
      location.hash = "#/decision";
    });
  }
  return cell;
}

function rowCells(row: QueueRow, context: PanelContext): readonly HTMLElement[] {
  const inputs = row.trace?.inputs;
  const energy = energyFor(row);
  const plan = plannedFor(row);
  const aircraft = aircraftFor(row);
  const reason = reasonFor(row);
  return [
    cell(
      [
        el("span", { text: row.order.external_ref ?? "no external reference" }),
        sub(`order ${shortId(row.order.order_id)}`),
      ],
      true,
    ),
    cell([
      sub(`tenant ${shortId(row.order.tenant_id, 6)}`),
      sub(`mission ${shortId(row.order.mission_id)}`),
    ]),
    cell([
      el("span", { text: padName(inputs?.origin_facility_id) }),
      sub("synthetic node id; the bundle's fictional name is shown, never a real address"),
    ]),
    cell([el("span", { text: padName(inputs?.destination_facility_id) })]),
    cell([
      el("span", { text: formatWhen(inputs?.requested_deadline) }),
      sub(
        inputs === undefined
          ? "no deadline on record"
          : `earliest ${formatWhen(inputs.earliest_departure)}`,
      ),
    ]),
    cell([
      mono(
        shortId(aircraft),
        `aircraft id ${aircraft}; plan version ${plan?.plan_version_id ?? "none on record"}`,
      ),
      sub(
        plan === null ? "no plan version committed" : `strategy ${plan.strategy ?? "not stated"}`,
      ),
    ]),
    cell([el("span", { text: energy.value, title: energy.hint })]),
    cell([reasonCell(row, context)]),
    cell([
      el("span", { class: "mq-status", text: `status: ${row.order.status}` }),
      sub(`trace outcome: ${row.trace?.record.outcome ?? "no trace read"}`),
      sub(
        `priority: ${row.order.priority_class ?? inputs?.priority_class ?? "not stated"}; ${reason.bucket.id === "unmapped" ? "unmapped code" : `queue bucket from ${reason.bucket.from}`}`,
      ),
    ]),
  ];
}

function queueTable(
  rows: readonly QueueRow[],
  context: PanelContext,
  caption: string,
  label: string,
): HTMLElement {
  const head = el("tr");
  for (const text of [
    "customer",
    "tenant / mission",
    "origin",
    "destination",
    "deadline",
    "aircraft",
    "energy state",
    "plain-language reason",
    "ledger status",
  ]) {
    head.append(el("th", { text, scope: "col" }));
  }
  const body = el("tbody");
  for (const row of rows) {
    const tr = el("tr", {
      data: { blocked: String(isBlocked(row)), reason: reasonFor(row).bucket.id },
    });
    tr.append(...rowCells(row, context));
    body.append(tr);
  }
  const table = el("table", { class: "mq-table", aria: { label } });
  const headSection = el("thead");
  headSection.append(head);
  table.append(el("caption", { text: caption }), headSection, body);
  return table;
}

/* ------------------------------------------------------------------ panel */

function renderInto(host: HTMLElement, context: PanelContext, gathered: Gathered): void {
  const blocked = gathered.rows.filter(isBlocked);
  const moving = gathered.rows.filter((row) => !isBlocked(row));
  host.replaceChildren();

  host.append(
    paragraph(
      "Every order the ledger knows about, in two groups, with refused and unserved work drawn first. Nothing is filtered away: a mission the planner refused is a row on this screen, because the refusal is part of the result.",
      "mq-intro",
    ),
    el("p", {
      class: "mq-bucket-key",
      text: `Queue labels are the seven from docs/05 section 5.4 plus one for energy reserve, mapped from the wire code printed under each sentence. ${blocked.length} refused, held or unserved; ${moving.length} committed or accepted.`,
    }),
  );

  if (gathered.rows.length === 0) {
    host.append(
      emptyState(
        "the mission queue",
        "The order book answered with no orders. The queue is genuinely empty, not still loading.",
        "an order accepted by /v1/orders for this tenant - apps/sim-runner produces them.",
      ),
    );
  }
  if (blocked.length > 0) {
    host.append(
      sectionTitle(`Refused, held or unserved (${blocked.length})`, "mq-blocked-heading"),
      el("p", {
        class: "mq-group-note",
        text: "Press Tab to reach the first row's reason button, then Enter: the decision trace opens with the refusal already focused and its technical detail one key away.",
      }),
      queueTable(
        blocked,
        context,
        "Orders this run has not served, each with the reason on record",
        "refused and unserved orders",
      ),
    );
  }
  if (moving.length > 0) {
    host.append(
      sectionTitle(`Committed or accepted (${moving.length})`, "mq-moving-heading"),
      queueTable(
        moving,
        context,
        "Orders with a commitment or an accepted plan",
        "committed and accepted orders",
      ),
    );
  }

  if (gathered.skipped > 0) {
    host.append(
      el("p", {
        class: "mq-cap",
        text: `Only the first ${MAX_ROWS} of ${MAX_ROWS + gathered.skipped} orders were expanded with a decision trace, so the queue costs ${MAX_ROWS * 2} reads. ${gathered.skipped} orders are listed unexpanded above; select one to read its trace.`,
      }),
    );
  }
  if (gathered.failures.length > 0) {
    const list = el("ul", { class: "mq-partials-list" });
    list.append(...gathered.failures.map((message) => el("li", { text: message })));
    host.append(
      block("div", { class: "mq-partials", role: "alert" }, [
        el("h4", {
          class: "kit-state-heading",
          text: `${gathered.failures.length} decision traces did not answer`,
        }),
        el("p", {
          class: "kit-state",
          text: "Their rows show 'not stated by the API' rather than a guessed value. The rest of the queue is unaffected.",
        }),
        list,
      ]),
    );
  }
  if (gathered.meta !== null) {
    host.append(provenanceLine(context, gathered.meta));
  }
}

export const missionQueuePanel: Panel = {
  id: "mission-queue",
  slot: "missions.queue",
  title: "Mission queue",
  description:
    "Customer, origin, destination, deadline, assigned aircraft, energy state and the plain-language reason - refused work first.",
  mount(host, context) {
    host.dataset.panelRoot = "mission-queue";
    let disposed = false;
    host.append(
      loadingState(
        "the order book",
        "then one decision trace and one plan set per mission, which is where origin, destination, deadline and the reason come from.",
      ),
    );
    const run = (): void => {
      void gather(context.api).then(
        (gathered) => {
          if (disposed || !document.body.contains(host)) {
            return;
          }
          if (gathered.ordersFailed) {
            host.replaceChildren(
              errorState(
                {
                  kind: "network",
                  operation: "listOrders",
                  message: gathered.failures[0] ?? "the order book did not answer",
                },
                run,
              ),
            );
            return;
          }
          renderInto(host, context, gathered);
        },
        (cause: unknown) => {
          if (disposed) {
            return;
          }
          host.replaceChildren(
            errorState(
              {
                kind: "contract",
                operation: "listOrders",
                message: `the queue could not assemble the reads: ${String(cause)}`,
              },
              run,
            ),
          );
        },
      );
    };
    run();
    context.onDispose(() => {
      disposed = true;
    });
  },
};
