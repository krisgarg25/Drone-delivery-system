/**
 * Mission board - one mission's cards, wall-style.
 *
 * The queue answers "what is waiting and why"; the board answers "what is the whole story
 * on this one mission": the order, every plan version with its route hash, the
 * reservations the ledger holds, the telemetry count, and the decision's outcome - each as
 * its own card with its own provenance, because a plan version, a commitment and a
 * measurement are three different kinds of claim and must not be read as one row.
 *
 * Simulation only. Not authorised for flight.
 */

import type { Schemas } from "../lib/api/types";
import type { Panel, PanelContext } from "../shell/panels";
import {
  block,
  button,
  el,
  emptyState,
  errorState,
  field,
  formatWhen,
  loadingState,
  mono,
  paragraph,
  shortId,
  table,
  type Column,
} from "./support/kit";
import { reasonOfMission } from "./support/reasons";
import "./panels.css";

interface Board {
  readonly missionId: string;
  readonly order: Schemas["OrderResponse"] | null;
  readonly plans: readonly Schemas["PlanVersionResponse"][];
  readonly reservations: readonly Schemas["ReservationResponse"][];
  readonly observations: number;
  readonly trace: Schemas["DecisionTraceResponse"] | null;
  readonly failures: readonly string[];
}

const PLAN_COLUMNS: readonly Column<Schemas["PlanVersionResponse"]>[] = [
  { header: "rank", cell: (row) => (row.rank === undefined ? "-" : String(row.rank)) },
  { header: "aircraft", cell: (row) => shortId(row.aircraft_id ?? "not attached") },
  { header: "strategy", cell: (row) => row.strategy ?? "not stated" },
  { header: "energy margin", cell: (row) => `${String(row.energy_margin_wh)} Wh` },
  {
    header: "route hash",
    cell: (row) => `${row.route_hash.slice(0, 12)}…${row.route_hash.slice(-6)}`,
    hint: (row) => `full route_hash: ${row.route_hash}`,
  },
  {
    header: "kind",
    cell: (row) => `${row.source_kind}${row.authoritative ? " · ledger" : " · proposal"}`,
  },
  { header: "resource keys", cell: (row) => (row.resource_keys ?? []).join(", ") || "not stated" },
];

async function gather(
  context: PanelContext,
  missionId: string,
): Promise<{ ok: true; board: Board } | { ok: false; message: string }> {
  const api = context.api;
  const [ordersResult, plansResult, reservationsResult, observationsResult, traceResult] =
    await Promise.all([
      api.orders(),
      api.planVersions(missionId),
      api.missionReservations(missionId),
      api.observations(missionId),
      api.decisionTrace(missionId),
    ]);
  if (!plansResult.ok && !traceResult.ok) {
    return { ok: false, message: `${plansResult.error.operation}: ${plansResult.error.message}` };
  }
  const failures: string[] = [];
  for (const [subject, result] of [
    ["order book", ordersResult],
    ["plan versions", plansResult],
    ["reservations", reservationsResult],
    ["observations", observationsResult],
    ["decision trace", traceResult],
  ] as const) {
    if (!result.ok) {
      failures.push(`${subject}: ${result.error.message}`);
    }
  }
  const order = ordersResult.ok
    ? (ordersResult.data.items.find((entry) => entry.mission_id === missionId) ?? null)
    : null;
  return {
    ok: true,
    board: {
      missionId,
      order,
      plans: plansResult.ok ? plansResult.data.items : [],
      reservations: reservationsResult.ok ? reservationsResult.data.items : [],
      observations: observationsResult.ok ? observationsResult.data.items.length : 0,
      trace: traceResult.ok ? traceResult.data : null,
      failures,
    },
  };
}

function card(title: string, children: readonly Node[], testid: string): HTMLElement {
  return block("section", { class: "mb-card", data: { card: testid }, aria: { label: title } }, [
    el("h4", { class: "mb-card-heading", text: title }),
    ...children,
  ]);
}

function render(host: HTMLElement, context: PanelContext, board: Board): void {
  const reason = reasonOfMission(board.trace);
  const committed = board.reservations.filter((row) => row.state === "committed");
  host.replaceChildren(
    card(
      "Order",
      board.order === null
        ? [
            emptyState(
              "the order behind this mission",
              `No order in the book points at mission ${shortId(board.missionId)}, so this mission was not created by an order this build can read.`,
              "an OrderResponse whose mission_id matches the selection.",
            ),
          ]
        : [
            field("external reference", board.order.external_ref ?? "not stated"),
            field("status", board.order.status),
            field("priority class", board.order.priority_class ?? "not stated"),
            field("order id", board.order.order_id),
            field(
              "provenance",
              `${board.order.source_kind} · authoritative ${String(board.order.authoritative)}`,
            ),
          ],
      "order",
    ),
    card(
      "Decision",
      [
        field("outcome", board.trace?.record.outcome ?? "no trace read"),
        field(
          "plain-language reason",
          `${reason.bucket.plain} - ${reason.humanReason ?? "the trace carried no sentence"}`,
        ),
        field("wire code", reason.code),
        paragraph(
          "The full trace, ranking and technical details are on the decision trace screen.",
        ),
        button("Open the decision trace", () => {
          context.selection.patch({ missionId: board.missionId });
          location.hash = "#/decision";
        }),
      ],
      "decision",
    ),
    card(
      "Plan versions",
      board.plans.length === 0
        ? [
            emptyState(
              "plan versions",
              "The planner stored no plan version for this mission: there is nothing to commit, which is itself the finding.",
              "a PlanVersionResponse from /v1/missions/{id}/plan-versions.",
            ),
          ]
        : [
            table(
              board.plans,
              PLAN_COLUMNS,
              "plan versions proposed or committed for this mission",
            ),
          ],
      "plans",
    ),
    card(
      "Reservations",
      board.reservations.length === 0
        ? [mono("the ledger holds no resource for this mission")]
        : [
            field(
              "committed rows",
              `${String(committed.length)} of ${String(board.reservations.length)}`,
            ),
            ...board.reservations
              .slice(0, 8)
              .map((row) =>
                field(
                  `${row.resource_kind} · ${row.state}`,
                  `${row.resource_key} ${formatWhen(row.valid_from)} → ${formatWhen(row.valid_until)}`,
                ),
              ),
          ],
      "reservations",
    ),
    card(
      "Telemetry",
      [
        field("stored observations", `${String(board.observations)} frames on the frozen snapshot`),
        paragraph(
          board.observations === 0
            ? "With no observations there is no trajectory to replay: the planned route is all this mission has."
            : "Observations are measurements; plan versions are proposals; a commitment is a ledger row. The three are never merged into one line.",
        ),
        button("Step the replay", () => {
          context.selection.patch({ missionId: board.missionId });
          location.hash = "#/replay";
        }),
      ],
      "telemetry",
    ),
  );
  if (board.failures.length > 0) {
    host.append(
      el("p", {
        class: "dt-partials",
        role: "status",
        text: `Sources that did not answer: ${board.failures.join(" | ")}`,
      }),
    );
  }
}

export const missionBoardPanel: Panel = {
  id: "mission-board",
  slot: "missions.board",
  title: "Mission board",
  description:
    "One mission as five cards: the order, the decision, the plan versions, the reservations the ledger holds, and what telemetry says happened.",
  mount(host, context) {
    host.dataset.panelRoot = "mission-board";
    let disposed = false;
    const missionId = context.selection.get().missionId;
    if (missionId === null) {
      host.replaceChildren(
        emptyState(
          "the mission board",
          "No mission is in scope. The board is deliberately not a wall of every mission at once - that is the queue's job.",
          "a mission picked from the queue or the map.",
        ),
      );
      void context.api.orders().then((result) => {
        if (
          disposed ||
          !result.ok ||
          result.data.items.length === 0 ||
          !document.body.contains(host)
        ) {
          return;
        }
        host.append(
          block("div", { class: "mb-select" }, [
            mono(
              `the order book holds ${String(result.data.items.length)} orders - the first is ${shortId(result.data.items[0].mission_id)}`,
            ),
            button("Put the first order on the board", () => {
              context.selection.patch({ missionId: result.data.items[0].mission_id });
              context.announce("Mission board now shows the first order in the book.");
            }),
          ]),
        );
      });
      return;
    }
    host.append(
      loadingState(
        `the mission board for ${shortId(missionId)}`,
        "order, decision, plan versions, reservations and telemetry count.",
      ),
    );
    void gather(context, missionId).then((result) => {
      if (disposed || !document.body.contains(host)) {
        return;
      }
      if (!result.ok) {
        host.replaceChildren(
          errorState(
            { kind: "contract", operation: "listMissionPlanVersions", message: result.message },
            () => {
              void gather(context, missionId).then((again) => {
                if (!disposed && again.ok) {
                  render(host, context, again.board);
                }
              });
            },
          ),
        );
        return;
      }
      render(host, context, result.board);
    });
    context.onDispose(() => {
      disposed = true;
    });
  },
};
