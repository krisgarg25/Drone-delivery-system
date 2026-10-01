/**
 * Decision trace - the stored answer, not a re-derivation.
 *
 * Phase 4's acceptance criterion is that a test operator finds the cause of a rejection in
 * under 60 seconds, so this panel is ordered by how fast each block answers the question,
 * not by how the trace is shaped:
 *
 *   1. the refusal sentence, focused on arrival from the queue, with its wire code;
 *   2. every rejected candidate, each with a human reason and a technical detail drawer;
 *   3. the numbered steps of the commit transaction;
 *   4. the ranking numbers that produced the choice, and the eligible fleet behind them;
 *   5. the inputs the planner was shown, the protected volumes, the commit, the config
 *      release hash and the replay identity.
 *
 * Everything here is read. The console does not re-run the planner, re-rank candidates or
 * re-check a reservation: Phase 4's exit criteria forbid browser-only scheduling and
 * docs/conventions.md section 6 keeps the planner and the checker in separate builds.
 *
 * Simulation only. Not authorised for flight.
 */

import type { Api } from "../lib/api/endpoints";
import type { ApiError, ResponseMeta } from "../lib/api/client";
import type { Schemas } from "../lib/api/types";
import { WORLD } from "../map/world";
import type { Panel, PanelContext } from "../shell/panels";
import {
  block,
  button,
  drawerButton,
  el,
  emptyState,
  field,
  fieldList,
  formatWhen,
  hashLine,
  loadingState,
  mono,
  paragraph,
  provenanceLine,
  sectionTitle,
  setHandoff,
  shortId,
  table,
  takeHandoff,
  type Column,
  type DrawerField,
} from "./support/kit";
import { explainReason, reasonOfMission } from "./support/reasons";
import "./panels.css";

interface TraceBundle {
  readonly missionId: string;
  readonly trace: Schemas["DecisionTraceResponse"];
  readonly reservations: readonly Schemas["ReservationResponse"][];
  readonly plans: readonly Schemas["PlanVersionResponse"][];
  readonly uncertainty: Schemas["UncertaintyStateResponse"] | null;
  readonly run: Schemas["SimulationRunResponse"] | null;
  readonly failures: readonly string[];
  readonly meta: ResponseMeta;
}

type Gathered =
  | { readonly kind: "ready"; readonly bundle: TraceBundle }
  | { readonly kind: "failed"; readonly error: ApiError };

const MAX_STEP = 20;

async function gather(api: Api, missionId: string, scenarioId: string): Promise<Gathered> {
  const traceResult = await api.decisionTrace(missionId);
  if (!traceResult.ok) {
    return { kind: "failed", error: traceResult.error };
  }
  const [reservations, plans, uncertainty, run] = await Promise.all([
    api.missionReservations(missionId),
    api.planVersions(missionId),
    api.uncertainty(missionId),
    api.simulationRun(scenarioId),
  ]);
  const failures: string[] = [];
  for (const [subject, result] of [
    ["mission reservations", reservations],
    ["plan versions", plans],
    ["uncertainty state", uncertainty],
    ["simulation run", run],
  ] as const) {
    if (!result.ok) {
      failures.push(`${subject}: ${result.error.operation} - ${result.error.message}`);
    }
  }
  return {
    kind: "ready",
    bundle: {
      missionId,
      trace: traceResult.data,
      reservations: reservations.ok ? reservations.data.items : [],
      plans: plans.ok ? plans.data.items : [],
      uncertainty: uncertainty.ok ? uncertainty.data : null,
      run: run.ok ? run.data : null,
      failures,
      meta: traceResult.meta,
    },
  };
}

function wasCommitted(trace: Schemas["DecisionTraceResponse"]): boolean {
  return trace.record.committed ?? trace.record.outcome === "committed";
}

/* ------------------------------------------------------------------ the answer */

function rejectionFields(
  rejection: Schemas["RejectedCandidate"],
  trace: Schemas["DecisionTraceResponse"],
): DrawerField[] {
  return [
    {
      label: "wire reason code",
      value: rejection.reason_code,
      hint: "the RejectionCode or dispatcher ReasonCode, printed exactly as the ledger stored it",
    },
    {
      label: "technical detail",
      value: rejection.technical_detail,
      hint: "the evidence behind the sentence",
    },
    {
      label: "step",
      value:
        rejection.step === undefined
          ? "not stated"
          : `step ${rejection.step} of the commit transaction`,
    },
    { label: "step name", value: rejection.step_name ?? "not stated" },
    {
      label: "resource key",
      value: rejection.resource_key ?? "not stated",
      hint: "the corridor, pad, charger bay or volume the refusal is about",
    },
    { label: "candidate", value: rejection.candidate_id ?? "not stated" },
    { label: "aircraft", value: rejection.aircraft_id ?? "no tail attached to this refusal" },
    { label: "config release", value: trace.record.config_release_sha },
    { label: "all codes on the record", value: trace.record.reason_codes.join(", ") || "none" },
  ];
}

function answerBlock(bundle: TraceBundle, context: PanelContext, host: HTMLElement): HTMLElement {
  const trace = bundle.trace;
  const rejections = trace.rejected ?? [];
  const first = rejections.at(0);
  const reason = reasonOfMission(trace);
  const committed = wasCommitted(trace);
  const meta = block("div", { class: "dt-answer-meta" }, [
    mono(`code: ${trace.primary_reason_code ?? reason.code}`),
    mono(`outcome: ${trace.record.outcome ?? "not stated"} · committed: ${String(committed)}`),
    mono(`decided by: ${trace.record.deciding_actor ?? "not stated"}`),
    mono(`recorded: ${formatWhen(trace.record.created_at)}`),
    context.stateChip(committed ? "committed" : "rejected", `queue label: ${reason.bucket.plain}`),
  ]);
  const refusal = el("h4", {
    class: "dt-answer-heading",
    id: "dt-answer-heading",
    text: committed
      ? `Committed. ${trace.primary_human_reason ?? "The ledger accepted this plan."}`
      : `Refused: ${reason.bucket.plain}`,
  });
  /*
   * The refusal is the sentence an operator came for, so it is a tab stop and not only a
   * heading. As a plain `h4` it could be skipped entirely by an operator tabbing past the
   * navigation, which is how the audit found the rejection unreachable by keyboard (F-1).
   * It sits immediately before its own technical-detail trigger, so the pair is read in order.
   */
  refusal.tabIndex = 0;
  const children: Node[] = [
    refusal,
    el("p", {
      class: "dt-answer-sentence",
      text:
        trace.primary_human_reason ??
        first?.human_reason ??
        "This trace carries no sentence, so the numbered steps below are the explanation.",
    }),
    meta,
  ];
  if (first !== undefined) {
    children.push(
      drawerButton(
        context,
        host,
        {
          title: `Technical detail - ${first.reason_code}`,
          subtitle: "The evidence the ledger recorded beside the sentence a human reads.",
          state: "rejected",
          fields: rejectionFields(first, trace),
          paragraphs: [
            "This drawer shows the trace the server stored. Nothing in it was recomputed here: the console has no planner, no checker and no reservation logic.",
            `The queue label "${reason.bucket.plain}" groups the wire code (src/panels/support/reasons.ts). The sentence above is the API's own.`,
          ],
        },
        "Open the technical detail",
      ),
    );
  }
  children.push(
    el("p", {
      class: "dt-read-time",
      text:
        trace.explanation_readable_in_s === undefined
          ? "The trace carries no readability figure for this mission."
          : `The server records this explanation as readable in ${trace.explanation_readable_in_s} s - the Phase 4 criterion this block is built to meet.`,
    }),
    button(
      "Step the replay for this mission",
      () => {
        context.selection.patch({ missionId: bundle.missionId });
        setHandoff("replay", bundle.missionId);
        context.announce("Opening the replay for this mission. Left and right arrow keys step it.");
        location.hash = "#/replay";
      },
      {
        primary: true,
        ariaLabel:
          "Step the replay for this mission. Opens the replay screen for the same mission.",
      },
    ),
  );
  return block(
    "section",
    { class: "dt-answer", aria: { label: "why this decision went the way it did" } },
    children,
  );
}

/* ------------------------------------------------------------------ sections */

function rejectionList(bundle: TraceBundle, context: PanelContext, host: HTMLElement): HTMLElement {
  const trace = bundle.trace;
  const rejections = trace.rejected ?? [];
  const wrap = el("div", { class: "dt-rejections" });
  wrap.append(sectionTitle(`Rejected candidates (${rejections.length}), each with its reason`));
  if (rejections.length === 0) {
    wrap.append(
      emptyState(
        "rejected candidates",
        "This trace records no rejected candidate: the transaction refused nothing. That is a result, not a missing row.",
        "a candidate the ledger or the dispatcher turned down - the mission queue lists the missions that do carry one.",
      ),
    );
    return wrap;
  }
  const items = el("ul", { class: "dt-rejection-list" });
  rejections.forEach((rejection, index) => {
    const reason = explainReason(rejection.reason_code, rejection.human_reason);
    items.append(
      block("li", { class: "dt-rejection", data: { code: rejection.reason_code } }, [
        el("span", {
          class: "dt-rejection-index",
          text: `rejection ${index + 1} of ${rejections.length}`,
        }),
        el("p", { class: "dt-rejection-human", text: rejection.human_reason }),
        block("p", { class: "dt-rejection-code" }, [
          mono(reason.code),
          mono(`queue label: ${reason.bucket.plain}`),
          mono(rejection.step_name ?? "no step named"),
        ]),
        drawerButton(
          context,
          host,
          {
            title: `Technical detail - ${reason.code}`,
            subtitle: rejection.human_reason,
            state: "rejected",
            fields: rejectionFields(rejection, trace),
            paragraphs: [
              "The technical detail is the evidence the transaction recorded: the resource key it examined, the step of the commit that refused it, and the configuration that was live at the time.",
            ],
          },
          "Technical detail",
        ),
      ]),
    );
  });
  wrap.append(items);
  return wrap;
}

type TraceStep = Schemas["DecisionTraceResponse"]["steps"][number];

function stepList(steps: readonly TraceStep[]): HTMLElement {
  const shown = steps.slice(0, MAX_STEP);
  const nodes: Node[] = [
    table(
      shown,
      [
        { header: "step", cell: (row) => (row.step === undefined ? "-" : String(row.step)) },
        { header: "stage", cell: (row) => row.stage },
        { header: "outcome", cell: (row) => row.outcome },
        {
          header: "detail",
          cell: (row) => row.detail ?? "no detail recorded",
          hint: () =>
            "the ledger's own words for this step; 'none' means the step recorded nothing",
        },
        {
          header: "numbers",
          cell: (row) => {
            const parts: string[] = [];
            if (row.energy_margin_wh !== undefined) {
              parts.push(`energy ${row.energy_margin_wh} Wh`);
            }
            if (row.timing_uncertainty_s !== undefined) {
              parts.push(`timing uncertainty ${row.timing_uncertainty_s} s`);
            }
            return parts.length === 0 ? "-" : parts.join(" · ");
          },
        },
      ] as readonly Column<TraceStep>[],
      "the commit transaction, step by step",
    ),
  ];
  if (steps.length > shown.length) {
    nodes.push(
      el("p", {
        class: "kit-state-hint",
        text: `${steps.length - shown.length} later steps are collapsed behind "show all ${steps.length} steps".`,
      }),
    );
  }
  return block("div", { class: "dt-steps" }, nodes);
}

function inputsSection(trace: Schemas["DecisionTraceResponse"]): HTMLElement {
  const inputs = trace.inputs;
  if (inputs === undefined) {
    return emptyState(
      "decision inputs",
      "This trace was stored without an inputs block, so the console cannot show what the planner was shown.",
      "a DecisionTraceResponse.inputs object from /v1/missions/{id}/decision.",
    );
  }
  return fieldList(
    [
      field("snapshot digest", inputs.snapshot_digest, "the frozen world the decision read"),
      field("policy version", inputs.policy_version),
      field("config release", inputs.config_release_sha),
      field("origin", inputs.origin_facility_id, "synthetic node id, never a real address"),
      field(
        "destination",
        inputs.destination_facility_id,
        "synthetic node id, never a real address",
      ),
      field("priority class", inputs.priority_class),
      field("earliest departure", formatWhen(inputs.earliest_departure)),
      field("requested deadline", formatWhen(inputs.requested_deadline)),
      field(
        "reservation version",
        String(inputs.reservation_version),
        "the ledger version the decision was taken against",
      ),
      field("restricted keys", (inputs.restricted_keys ?? []).join(", ") || "none"),
      field("world digest at decision", inputs.world_digest_at_decision ?? "not stated"),
    ],
    "exactly what the planner was shown",
  );
}

const ELIGIBLE_COLUMNS: readonly Column<Schemas["EligibleAircraft"]>[] = [
  { header: "rank", cell: (row) => String(row.rank) },
  { header: "aircraft", cell: (row) => shortId(row.aircraft_id) },
  {
    header: "ledger status",
    cell: (row) => row.status,
    hint: () =>
      "the aircraft state the ledger held at decision time; connected is not the same as available",
  },
  { header: "link", cell: (row) => row.link_quality },
  { header: "energy margin Wh", cell: (row) => String(row.energy_margin_wh) },
  { header: "recovery margin Wh", cell: (row) => String(row.recovery_margin_wh) },
  {
    header: "lateness",
    cell: (row) => (row.lateness_us === 0 ? "on time" : `${row.lateness_us} µs`),
  },
  { header: "disturbance", cell: (row) => String(row.disturbance_units) },
  { header: "fairness", cell: (row) => String(row.fairness_units) },
  { header: "total units", cell: (row) => String(row.total_units) },
];

const RANKING_COLUMNS: readonly Column<Schemas["CandidateRanking"]>[] = [
  { header: "rank", cell: (row) => String(row.rank) },
  { header: "candidate", cell: (row) => row.candidate_id },
  { header: "aircraft", cell: (row) => shortId(row.aircraft_id) },
  { header: "strategy", cell: (row) => row.strategy },
  { header: "total units", cell: (row) => String(row.total_units) },
  { header: "lateness µs", cell: (row) => String(row.lateness_us) },
  { header: "energy margin Wh", cell: (row) => String(row.energy_margin_wh) },
];

const RESERVATION_COLUMNS: readonly Column<Schemas["ReservationResponse"]>[] = [
  { header: "resource key", cell: (row) => row.resource_key },
  { header: "kind", cell: (row) => row.resource_kind },
  { header: "state", cell: (row) => row.state },
  { header: "from", cell: (row) => formatWhen(row.valid_from) },
  { header: "until", cell: (row) => formatWhen(row.valid_until) },
  { header: "commitment token", cell: (row) => row.commitment_token ?? "none recorded" },
  {
    header: "provenance",
    cell: (row) => `${row.source_kind} · authoritative ${String(row.authoritative)}`,
  },
];

function volumesSection(bundle: TraceBundle): HTMLElement {
  const volumes = bundle.reservations;
  if (volumes.length === 0) {
    return emptyState(
      "protected volumes and reservations",
      "The ledger holds no reservation rows for this mission, so nothing is protected and nothing is committed.",
      "a committed or tentative reservation from /v1/missions/{id}/reservations.",
    );
  }
  const retained = volumes.filter((row) => row.state === "unresolved");
  return block("div", { class: "dt-volumes" }, [
    paragraph(
      `${volumes.length} reservation rows for this mission; ${retained.length} unresolved, which means the volume stays held until telemetry reconciles. A stale aircraft never releases its airspace (technical-build/01 section 14).`,
    ),
    table(volumes, RESERVATION_COLUMNS, "reservations held for this mission"),
  ]);
}

function commitSection(bundle: TraceBundle, context: PanelContext): HTMLElement {
  const trace = bundle.trace;
  const committed = wasCommitted(trace);
  const rows: HTMLElement[] = [
    field(
      "commitment",
      committed ? "committed in the ledger transaction" : "not committed",
      "record.committed and record.outcome as the API reported them; this panel does not decide what counts as committed",
    ),
    field("selected plan version", trace.selected_plan_version_id ?? "none selected"),
    field(
      "lock order",
      (trace.lock_order ?? []).join(" → ") || "not stated",
      "the canonical resource order the transaction took its locks in",
    ),
    ...bundle.plans.map((plan) =>
      field(
        `plan version rank ${plan.rank ?? "-"}`,
        `${shortId(plan.plan_version_id)} · ${plan.energy_margin_wh} Wh · ${plan.source_kind}`,
        `route_hash ${plan.route_hash} · config ${plan.config_release_sha} · snapshot ${plan.snapshot_digest ?? "not stated"}`,
      ),
    ),
  ];
  const wrap = el("div", { class: "dt-commit" });
  wrap.append(fieldList(rows, "reservation commit and the plan versions behind it"));
  if (bundle.uncertainty !== null) {
    const uncertainty = bundle.uncertainty;
    wrap.append(
      sectionTitle("Uncertainty for the aircraft in scope"),
      fieldList(
        [
          field("state", uncertainty.state, "clear, uncertain, lost or reconciling"),
          field(
            "staleness",
            uncertainty.staleness_s === undefined ? "not stated" : `${uncertainty.staleness_s} s`,
          ),
          field(
            "last accepted measurement",
            formatWhen(uncertainty.last_accepted_measurement_time),
          ),
          field("protected volume retained", String(uncertainty.protected_volume_retained)),
        ],
        "uncertainty state",
      ),
      context.stateChip(uncertainty.state === "clear" ? "observed" : "uncertain"),
    );
  }
  return wrap;
}

function identitySection(bundle: TraceBundle): HTMLElement {
  const trace = bundle.trace;
  const identity = bundle.run?.run_identity;
  const rows: HTMLElement[] = [
    hashLine("config release hash", trace.record.config_release_sha),
    hashLine("input hash", trace.record.input_hash ?? "not stated by the trace"),
    hashLine("snapshot digest", trace.inputs?.snapshot_digest ?? "not stated"),
    hashLine("world digest at decision", trace.inputs?.world_digest_at_decision ?? "not stated"),
    hashLine("bundle manifest", WORLD.provenance.bundle_manifest_sha256),
    field("scenario", WORLD.provenance.scenario_id, "the frozen bundle this console is pointed at"),
    field("seed", String(WORLD.provenance.seed), "reproduce with `make replay`"),
    field("policy version", trace.inputs?.policy_version ?? WORLD.provenance.policy_version),
  ];
  if (identity === undefined) {
    rows.push(
      field(
        "run identity",
        "not read - getSimulationRun did not answer for this scenario",
        "the replay panel reports the same gap. A run identity is the only thing that makes a trace reproducible, so its absence is shown rather than papered over.",
      ),
    );
  } else {
    rows.push(
      field("engine", identity.engine_version),
      field("scenario sha256", identity.scenario_sha256),
      field("run seed", String(identity.seed)),
      field(
        "python / architecture",
        `${identity.python_version} · ${identity.machine_architecture}`,
      ),
      field("event hash", bundle.run?.event_hash ?? "not stated"),
      field("frozen at", formatWhen(bundle.run?.frozen_at)),
    );
  }
  return block("div", { class: "dt-identity" }, [
    paragraph(
      "Replay identity: the hashes a reproduction has to match. A trace without them is an anecdote, which is why they are printed even when the commitment succeeded.",
    ),
    fieldList(rows, "replay identity"),
  ]);
}

/* ------------------------------------------------------------------ rendering */

function render(
  host: HTMLElement,
  context: PanelContext,
  bundle: TraceBundle,
  missionId: string,
): void {
  const trace = bundle.trace;
  host.replaceChildren();
  host.append(
    el("p", {
      class: "dt-scope",
      text: `Mission ${shortId(missionId)} · plan versions ${bundle.plans.length} · reservations ${bundle.reservations.length} · rejected candidates ${(trace.rejected ?? []).length} · committed ${String(wasCommitted(trace))}`,
    }),
    answerBlock(bundle, context, host),
    rejectionList(bundle, context, host),
    sectionTitle("Where the decision went wrong, step by step"),
    stepList(trace.steps),
    sectionTitle("Eligible aircraft, with the numbers that ranked them"),
    trace.eligible === undefined || trace.eligible.length === 0
      ? emptyState(
          "eligible aircraft",
          "The trace recorded no eligible-aircraft list, so the ranking cannot be shown either.",
          "an `eligible` array on the decision trace.",
        )
      : eligibleTable(trace.eligible),
    sectionTitle("Candidate ranking"),
    trace.ranking === undefined || trace.ranking.length === 0
      ? emptyState(
          "candidate ranking",
          "The trace recorded no ranked candidates.",
          "a `ranking` array of CandidateRanking rows.",
        )
      : rankingTable(trace.ranking),
    sectionTitle("What the planner was shown"),
    inputsSection(trace),
    sectionTitle("Protected volumes and the reservation commit"),
    volumesSection(bundle),
    commitSection(bundle, context),
    sectionTitle("Config release and replay identity"),
    identitySection(bundle),
  );
  host.append(provenanceLine(context, bundle.meta));
  if (bundle.failures.length > 0) {
    host.append(
      el("p", {
        class: "dt-partials",
        role: "status",
        text: `Partial reads: ${bundle.failures.join(" | ")}. Those blocks print as missing instead of borrowing data from a neighbour.`,
      }),
    );
  }
  if (takeHandoff("decision-trace", missionId)) {
    const heading = document.getElementById("dt-answer-heading");
    if (heading instanceof HTMLElement) {
      announceArrival(context, heading, trace);
    }
  }
}

function announceArrival(
  context: PanelContext,
  heading: HTMLElement,
  trace: Schemas["DecisionTraceResponse"],
): void {
  heading.tabIndex = 0;
  heading.scrollIntoView({
    block: "nearest",
    behavior: context.reducedMotion() ? "auto" : "smooth",
  });
  heading.focus();
  const count = (trace.rejected ?? []).length;
  const reason = reasonOfMission(trace);
  context.announce(
    `Decision trace open. ${
      count === 0
        ? "No candidate was rejected."
        : `${count} rejection${count === 1 ? "" : "s"}. ${trace.primary_human_reason ?? reason.bucket.plain}`
    } This refusal is a tab stop: one more Tab opens its technical detail, then the replay stepper.`,
  );
}

function eligibleTable(rows: readonly Schemas["EligibleAircraft"][]): HTMLElement {
  return table(rows, ELIGIBLE_COLUMNS, "eligible aircraft with the numbers that ranked them");
}

function rankingTable(rows: readonly Schemas["CandidateRanking"][]): HTMLElement {
  return table(rows, RANKING_COLUMNS, "candidate ranking as the server produced it");
}

/* ------------------------------------------------------------------ panel */

export const decisionTracePanel: Panel = {
  id: "decision-trace",
  slot: "decision.trace",
  title: "Decision trace",
  description:
    "The refusal first, then every rejected candidate with a human reason and a technical detail drawer, then the ranking, inputs, volumes, commit and replay identity.",
  mount(host, context) {
    host.dataset.panelRoot = "decision-trace";
    const missionId = context.selection.get().missionId;
    let disposed = false;

    if (missionId === null) {
      host.replaceChildren(
        emptyState(
          "the decision trace",
          "No mission is in scope, so there is nothing to trace. This screen shows one mission's stored decision, and the mission is picked in the queue or on the map.",
          "a selection: a refusal's button in the mission queue, or a mission picked on the map.",
        ),
        button(
          "Go to the mission queue",
          () => {
            location.hash = "#/missions";
          },
          { ariaLabel: "Go to the mission queue and pick a refused mission" },
        ),
      );
      return;
    }

    host.append(
      loadingState(
        `the decision trace for mission ${shortId(missionId)}`,
        "the trace, its reservations, its plan versions, its uncertainty state and the run identity.",
      ),
    );

    const show = (node: HTMLElement): void => {
      if (!disposed && document.body.contains(host)) {
        host.replaceChildren(node);
      }
    };

    void gather(context.api, missionId, context.selection.get().scenarioId).then(
      (gathered) => {
        if (disposed || !document.body.contains(host)) {
          return;
        }
        if (gathered.kind === "failed") {
          const error = gathered.error;
          show(
            block("div", { class: "dt-error" }, [
              sectionTitle("This trace could not be read"),
              el("p", {
                class: "kit-state kit-state--error",
                role: "alert",
                text: `${error.operation}: ${error.kind} - ${error.message}`,
              }),
              paragraph(
                "The console does not reconstruct a decision from other sources. A missing trace is shown as missing: a guessed reason is worse than no reason.",
              ),
              button("Read it again", () => {
                context.selection.patch({ missionId });
                location.hash = "#/decision";
              }),
            ]),
          );
          return;
        }
        render(host, context, gathered.bundle, missionId);
      },
      (cause: unknown) => {
        show(
          el("p", {
            class: "kit-state kit-state--error",
            role: "alert",
            text: `The trace request threw instead of answering: ${String(cause)}`,
          }),
        );
      },
    );

    context.onDispose(() => {
      disposed = true;
    });
  },
};
