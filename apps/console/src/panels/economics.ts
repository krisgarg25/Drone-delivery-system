/**
 * Economics view - every number labelled illustrative, with its formula and denominator
 * on the card and a drawer carrying the definition.
 *
 * docs/05-chandigarh-demo.md 5.4 asks for nine figures and 5.8/5.9 define them. The read
 * contract carries some of them and not others, and this panel draws that line in the open:
 * a figure the API reported appears with its endpoint and field; a figure the API does not
 * report appears with the formula that would produce it, the denominator that would divide
 * it, and the words "not reported by this build's read API". The console does not estimate
 * a missing economic quantity - an invented cost per delivery is exactly the kind of claim
 * a demo like this must not make.
 *
 * The only arithmetic performed here is on two integers the same response already
 * reported (completion and rejection share a denominator), and it is printed beside both
 * inputs so nobody has to take it on trust.
 *
 * Simulation only. Not authorised for flight.
 */

import type { ApiError } from "../lib/api/client";
import type { Schemas } from "../lib/api/types";
import { WORLD } from "../map/world";
import type { Panel, PanelContext } from "../shell/panels";
import {
  block,
  drawerButton,
  el,
  emptyState,
  errorState,
  field,
  fieldList,
  loadingState,
  mono,
  paragraph,
  provenanceLine,
  sectionTitle,
  table,
  type Column,
  type DrawerField,
} from "./support/kit";
import "./panels.css";

interface Metric {
  readonly key: string;
  readonly label: string;
  readonly value: string;
  readonly reported: boolean;
  readonly formula: string;
  readonly denominator: string;
  readonly source: string;
  readonly caveat: string;
  /** The read plane's own `illustrative` flag for the field, when it sent one. */
  readonly illustrative?: boolean;
}

/**
 * One figure as `EconomicsMetricResponse` sends it (apps/api/src/droneops_api/schemas.py).
 *
 * The console's committed `src/generated/api-types.ts` predates the field - regenerating it
 * now would ride on an in-flight contract change - so the shape is declared here and read
 * through a runtime check. Nothing is invented at this boundary: a metric the response does
 * not carry stays `null`, and the card keeps its "not reported" wording.
 */
interface EconomicsMetricWire {
  readonly key?: string;
  readonly value?: string;
  readonly unit?: string;
  readonly formula?: string;
  readonly denominator?: string;
  readonly source?: string;
  readonly illustrative?: boolean;
}

interface EconomicsWire {
  readonly metrics?: readonly EconomicsMetricWire[];
}

/** The figure the read plane reported, with the definitions that travelled with it. */
interface WireRead {
  readonly value: string | null;
  readonly formula: string;
  readonly denominator: string;
  readonly source: string;
  readonly illustrative: boolean;
}

function economicsOf(run: Schemas["SimulationRunResponse"] | null): EconomicsWire | null {
  if (run === null) {
    return null;
  }
  const candidate: unknown = (run as unknown as { economics?: unknown }).economics;
  if (typeof candidate !== "object" || candidate === null) {
    return null;
  }
  return candidate as EconomicsWire;
}

/**
 * A figure the API reported is drawn with the API's own formula, denominator and source, so
 * the card never paraphrases the definition it is quoting (audit F-6's "hard-coded not
 * reported" strings for charger wait, controller/pilot time and the road baseline).
 */
function wireReader(metrics: ReadonlyMap<string, EconomicsMetricWire>) {
  return (key: string, fallback: Omit<WireRead, "value" | "illustrative">): WireRead => {
    const entry = metrics.get(key);
    const reported: string | null =
      typeof entry?.value === "string" && entry.value !== "" ? entry.value : null;
    return {
      value: reported,
      formula: reported !== null ? (entry?.formula ?? fallback.formula) : fallback.formula,
      denominator:
        reported !== null ? (entry?.denominator ?? fallback.denominator) : fallback.denominator,
      source:
        reported !== null
          ? entry?.source !== undefined
            ? `getSimulationRun -> economics.metrics[${key}] (${entry.source})`
            : `getSimulationRun -> economics.metrics[${key}]`
          : fallback.source,
      illustrative: entry?.illustrative === true,
    };
  };
}

interface EconData {
  readonly run: Schemas["SimulationRunResponse"] | null;
  readonly comparison: Schemas["ScenarioComparisonResponse"] | null;
  readonly orders: number;
  readonly failures: readonly string[];
  readonly metaSource: string;
}

function metricsFor(data: EconData): Metric[] {
  const run = data.run;
  const comparisonMetric = (key: string): string | null =>
    data.comparison?.compared_metrics?.[key] ?? null;
  const wireEntries: [string, EconomicsMetricWire][] = [];
  for (const entry of economicsOf(data.run)?.metrics ?? []) {
    if (typeof entry.key === "string") {
      wireEntries.push([entry.key, entry]);
    }
  }
  const read = wireReader(new Map(wireEntries));
  /** Economics metrics first, then the comparison map, then nothing - never a guess. */
  const figure = (key: string, fallback: Omit<WireRead, "value" | "illustrative">): WireRead => {
    const fromWire = read(key, fallback);
    if (fromWire.value !== null) {
      return fromWire;
    }
    const fromComparison = comparisonMetric(key);
    return fromComparison === null
      ? fromWire
      : {
          ...fromWire,
          value: fromComparison,
          illustrative: true,
          source: `compareScenarios -> compared_metrics[${key}]`,
        };
  };
  const submitted = run?.submitted_orders ?? null;
  const accepted = run?.accepted ?? null;
  const rejected = run?.rejected ?? null;
  const completion =
    submitted !== null && accepted !== null && submitted > 0
      ? `${((accepted / submitted) * 100).toFixed(1)}%`
      : "not computable from what was reported";
  const energy = figure("energy_per_completion_wh", {
    formula:
      "all fleet energy, including failed and repositioning flights, divided by completed orders",
    denominator: "completed orders - the same upper-bound count as above",
    source: "compareScenarios -> compared_metrics[energy_per_completion_wh]",
  });
  const padWait = figure("pad_wait_us", {
    formula:
      "time an aircraft is airborne or queued while its destination pad is unavailable, per mission",
    denominator: "missions that requested a pad in the window",
    source: "the dispatcher reports pad_wait_us in its comparison summary; /v1 does not expose it",
  });
  const chargerWait = figure("charger_wait_us", {
    formula: "time an aircraft spends waiting for a charger bay, per mission",
    denominator: "missions whose aircraft needed a bay",
    source: "charger_bay reservations exist in the ledger; no read operation aggregates them",
  });
  const controllerTime = figure("controller_minutes_per_mission", {
    formula: "controller attention minutes attributable to a mission, divided by missions",
    denominator: "missions in the run",
    source: "no contract field",
  });
  const pilotTime = figure("pilot_time_minutes_per_mission", {
    formula: "pilot minutes attributable to a mission, divided by missions",
    denominator: "missions in the run",
    source: "no contract field",
  });
  const cost = figure("cost_per_completion", {
    formula:
      "aircraft and hub depreciation + batteries and maintenance + staff + energy + insurance + facility + repositioning and failed missions, divided by completed deliveries (docs/05 section 5.9)",
    denominator: "completed deliveries - the upper-bound figure above",
    source: "no contract field",
  });
  const baseline = figure("road_baseline", {
    formula: "modelled cost per completed delivery against the road baseline for the same lane",
    denominator: "completed deliveries",
    source: "no contract field",
  });
  const unserved = figure("unserved_rate", {
    formula:
      "rejected plus orders unfinished at the cutoff, without double counting (docs/05 section 5.8)",
    denominator: "all submitted orders",
    source: "getSimulationRun -> rejected, and a cutoff count the contract does not carry",
  });
  return [
    {
      key: "completed",
      label: "Completed orders",
      value: accepted === null ? "not reported" : String(accepted),
      reported: accepted !== null,
      formula: "count of orders whose mission reached a terminal served state",
      denominator: `the run's submitted demand: ${submitted ?? "not reported"} orders`,
      source: "getSimulationRun -> accepted",
      caveat:
        "The contract reports `accepted`, not `completed`. An accepted order may still be unfinished at the cutoff, so this figure is an upper bound on completions and is labelled as such wherever it is used.",
    },
    {
      key: "completion-rate",
      label: "Completion rate",
      value: accepted === null || submitted === null ? "not reported" : completion,
      reported: accepted !== null && submitted !== null,
      formula: `accepted / submitted = ${accepted ?? "?"} / ${submitted ?? "?"}`,
      denominator: "all submitted orders (docs/05 section 5.8)",
      source: "arithmetic on two integers from getSimulationRun",
      caveat:
        "The console divides two numbers the same response reported and prints both beside the result. It is not a scheduling decision: no order was ranked, assigned or validated here.",
    },
    {
      key: "on-time",
      label: "On-time rate",
      value: run === null ? "not reported" : `${(run.on_time_rate * 100).toFixed(1)}%`,
      reported: run !== null,
      formula: "completed before the requested deadline / orders carrying a deadline",
      denominator: "orders with deadlines, not all orders",
      source: "getSimulationRun -> on_time_rate",
      caveat:
        "The server computes it. An order with no deadline is outside the denominator, which is why this rate can sit far above the completion rate.",
    },
    {
      key: "unserved",
      label: "Unserved rate",
      value:
        unserved.value ??
        (rejected === null || submitted === null
          ? "not reported by this build's read API"
          : `at least ${((rejected / submitted) * 100).toFixed(1)}%`),
      reported: unserved.value !== null,
      formula: unserved.formula,
      denominator: unserved.denominator,
      source: unserved.source,
      illustrative: unserved.illustrative,
      caveat:
        unserved.value === null
          ? "Only the rejected half of the numerator is on the wire. Missions accepted and still unfinished at the cutoff are not exposed, so the figure shown is a floor, not the rate."
          : "The server reports this rate directly, so the floor the console used to print from `rejected` alone is gone.",
    },
    {
      key: "energy",
      label: "Energy per completion",
      value: energy.value ?? "not reported by this build's read API",
      reported: energy.value !== null,
      formula: energy.formula,
      denominator: energy.denominator,
      source: energy.source,
      illustrative: energy.illustrative,
      caveat:
        "The energy basis is an illustrative airframe profile from the versioned config, never a measured consumption. Repositioning and failed missions belong in the numerator by definition, which is what makes this figure higher than a per-flight average.",
    },
    {
      key: "pad-wait",
      label: "Pad wait",
      value: padWait.value ?? "not reported by this build's read API",
      reported: padWait.value !== null,
      formula: padWait.formula,
      denominator: padWait.denominator,
      source: padWait.source,
      illustrative: padWait.illustrative,
      caveat:
        "Kept separate from charger wait on purpose: a pad hold is a handover capacity problem, a charger hold is an energy problem, and fixing one does not fix the other.",
    },
    {
      key: "charger-wait",
      label: "Charger wait",
      value: chargerWait.value ?? "not reported by this build's read API",
      reported: chargerWait.value !== null,
      formula: chargerWait.formula,
      denominator: chargerWait.denominator,
      source: chargerWait.source,
      illustrative: chargerWait.illustrative,
      caveat:
        chargerWait.value === null
          ? "The raw material is on the wire - charger_bay reservation rows with valid_from and valid_until - but averaging them here would be computing a metric the server has not defined, so the panel names the gap instead."
          : "The read plane aggregates the charger_bay queue itself; the console prints that figure rather than averaging reservation rows in the browser.",
    },
    {
      key: "controller-time",
      label: "Controller time per mission",
      value: controllerTime.value ?? "not reported by this build's read API",
      reported: controllerTime.value !== null,
      formula: controllerTime.formula,
      denominator: controllerTime.denominator,
      source: controllerTime.source,
      illustrative: controllerTime.illustrative,
      caveat:
        controllerTime.value === null
          ? "docs/05 section 5.4 requires it and 5.8 requires airborne and productive time to be reported separately. There is no crew-time field in the contract, so nothing is shown."
          : "docs/05 section 5.4 asks for this figure; it now arrives on the read plane with its own denominator, so the card shows the reported value instead of the gap.",
    },
    {
      key: "pilot-time",
      label: "Pilot time per mission",
      value: pilotTime.value ?? "not reported by this build's read API",
      reported: pilotTime.value !== null,
      formula: pilotTime.formula,
      denominator: pilotTime.denominator,
      source: pilotTime.source,
      illustrative: pilotTime.illustrative,
      caveat:
        pilotTime.value === null
          ? "Crew reservations exist as a resource kind, but the ledger stores a slot and a window, not a duration of pilot attention."
          : "This is aircraft-held time per completed mission, as the server defines it - not a measured minute of a person's attention.",
    },
    {
      key: "cost",
      label: "Modelled cost per completed delivery",
      value: cost.value ?? "not reported by this build's read API",
      reported: cost.value !== null,
      formula: cost.formula,
      denominator: cost.denominator,
      source: cost.source,
      illustrative: cost.illustrative,
      caveat:
        "Section 5.9 requires the simulation to calculate it and requires it to be sensitive to deliveries per day per hub. It also states the illustrative model in chapter 7 remains unvalidated and requires real quotes, which is why the seven cost components are listed rather than summed.",
    },
    {
      key: "baseline",
      label: "Comparison to baseline road cost",
      value: baseline.value ?? "not reported by this build's read API",
      reported: baseline.value !== null,
      formula: baseline.formula,
      denominator: baseline.denominator,
      source: baseline.source,
      illustrative: baseline.illustrative,
      caveat:
        baseline.value === null
          ? "A road baseline is an external price series this build does not hold. Showing a ratio computed from one missing number and one invented one would be the single most misleading thing this screen could do."
          : "The read plane reports this ratio against an illustrative road-leg price band from docs/07 section 7.2 - a planning estimate inside this build, not a quoted market rate. It is labelled illustrative for exactly that reason.",
    },
  ];
}

function metricFields(metric: Metric, data: EconData): DrawerField[] {
  return [
    { label: "figure", value: metric.value },
    { label: "formula", value: metric.formula },
    { label: "denominator", value: metric.denominator },
    { label: "reported by", value: metric.source },
    { label: "run read from", value: data.metaSource },
    {
      label: "scenario",
      value: `${WORLD.provenance.scenario_id} (seed ${String(WORLD.provenance.seed)})`,
    },
    {
      label: "authoritative",
      value: String(data.run?.authoritative ?? false),
      hint: "the run's own claim about itself, from the contract field",
    },
  ];
}

/** Every card carries the illustrative label; the wording names why it is illustrative. */
function illustrativeTag(metric: Metric): string {
  return metric.illustrative === true
    ? "illustrative - modelled money, flagged by the read API, never quoted"
    : "illustrative - describes the frozen synthetic run";
}

const METRIC_COLUMNS: readonly Column<Metric>[] = [
  {
    header: "metric",
    cell: (row) => row.label,
    hint: (row) => `formula: ${row.formula}`,
  },
  { header: "value", cell: (row) => row.value },
  {
    header: "on the wire?",
    cell: (row) => (row.reported ? "reported by the API" : "not reported"),
  },
  { header: "formula", cell: (row) => row.formula },
  { header: "denominator", cell: (row) => row.denominator },
  { header: "source", cell: (row) => row.source },
];

function metricCards(
  metrics: readonly Metric[],
  data: EconData,
  context: PanelContext,
  host: HTMLElement,
): HTMLElement {
  const list = el("ul", { class: "ec-metrics" });
  for (const metric of metrics) {
    list.append(
      block(
        "li",
        {
          class: "ec-metric",
          data: { available: String(metric.reported) },
          title: `${metric.formula} / ${metric.denominator}`,
        },
        [
          el("span", { class: "ec-metric-key", text: metric.label }),
          el("span", { class: "ec-metric-value", text: metric.value }),
          el("span", { class: "ec-illustrative", text: illustrativeTag(metric) }),
          block("div", { class: "ec-metric-foot" }, [
            mono(metric.reported ? "reported field" : "not reported"),
            drawerButton(
              context,
              host,
              {
                title: `${metric.label} - how it is computed`,
                subtitle: metric.caveat,
                fields: metricFields(metric, data),
                paragraphs: [
                  metric.caveat,
                  "Every figure on this screen describes the frozen synthetic scenario. It is not an operating cost, a validated price, or evidence about real aircraft.",
                ],
              },
              "formula and denominator",
            ),
          ]),
        ],
      ),
    );
  }
  return list;
}

async function gather(
  api: PanelContext["api"],
): Promise<{ ok: true; data: EconData } | { ok: false; error: ApiError }> {
  const scenarioId = WORLD.provenance.scenario_id;
  const [runResult, comparisonResult, ordersResult] = await Promise.all([
    api.simulationRun(scenarioId),
    api.compareScenarios(scenarioId, `${scenarioId}-policy-b`),
    api.orders(),
  ]);
  if (!runResult.ok && !comparisonResult.ok) {
    return { ok: false, error: runResult.error };
  }
  const failures: string[] = [];
  if (!runResult.ok) {
    failures.push(`getSimulationRun: ${runResult.error.message}`);
  }
  if (!comparisonResult.ok) {
    failures.push(`compareScenarios: ${comparisonResult.error.message}`);
  }
  if (!ordersResult.ok) {
    failures.push(`listOrders: ${ordersResult.error.message}`);
  }
  return {
    ok: true,
    data: {
      run: runResult.ok ? runResult.data : null,
      comparison: comparisonResult.ok ? comparisonResult.data : null,
      orders: ordersResult.ok ? ordersResult.data.items.length : 0,
      failures,
      metaSource:
        [runResult.ok ? "getSimulationRun" : null, comparisonResult.ok ? "compareScenarios" : null]
          .filter((entry): entry is string => entry !== null)
          .join(", ") || "no economics source answered",
    },
  };
}

export const economicsPanel: Panel = {
  id: "economics",
  slot: "economics.metrics",
  title: "Economics of the run",
  description:
    "Completed work, on-time and unserved rate, energy per completion, pad and charger wait, crew time, modelled cost and the road baseline - each with its formula, denominator and availability.",
  mount(host, context) {
    host.dataset.panelRoot = "economics";
    let disposed = false;
    host.append(
      loadingState(
        "the run economics",
        "one simulation run, one scenario comparison and the order count for the denominator.",
      ),
    );
    const run = (): void => {
      void gather(context.api).then((result) => {
        if (disposed || !document.body.contains(host)) {
          return;
        }
        if (!result.ok) {
          host.replaceChildren(
            errorState(result.error, run),
            paragraph(
              "Economics has no fallback. If the run endpoint does not answer, this screen shows the failure rather than a figure assembled from a fixture.",
            ),
          );
          return;
        }
        const data = result.data;
        if (data.run === null && data.comparison === null) {
          host.replaceChildren(
            emptyState(
              "the run economics",
              "Neither getSimulationRun nor compareScenarios produced a run for this scenario, so there is no denominator to divide anything by.",
              "a recorded simulation run - apps/sim-runner writes them.",
            ),
          );
          return;
        }
        const metrics = metricsFor(data);
        host.replaceChildren(
          paragraph(
            "Every figure below is illustrative: it describes one frozen synthetic scenario, the monetary figures carry the read API's own illustrative flag, and a figure no read operation carries is drawn as missing rather than estimated. Hover a card for its formula and denominator, or open the drawer for the definition and its source field.",
          ),
          sectionTitle("Figures"),
          metricCards(metrics, data, context, host),
          sectionTitle("The same table with its definitions"),
          table(metrics, METRIC_COLUMNS, "economics metrics with formula, denominator and source"),
          fieldList(
            [
              field("reported by", data.metaSource),
              field(
                "scenario",
                `${WORLD.provenance.scenario_id}, seed ${String(WORLD.provenance.seed)}`,
              ),
              field(
                "energy basis",
                "illustrative airframe profile from the versioned config, never a measurement",
              ),
              field(
                "order book seen",
                `${data.orders} orders in the queue this screen can cross-check`,
              ),
            ],
            "scope of these figures",
          ),
        );
        if (data.failures.length > 0) {
          host.append(
            el("p", {
              class: "kit-state-hint",
              role: "status",
              text: `Sources that did not answer: ${data.failures.join(" | ")}.`,
            }),
          );
        }
        const meta = data.metaSource;
        host.append(
          el("p", {
            class: "kit-prov",
            text: `Economics read from ${meta}. Simulation only. Not authorised for flight.`,
          }),
          provenanceLine(context, {
            transportMode: context.api.transport.mode,
            transportLabel: context.api.transport.label,
            operation: "getSimulationRun",
            status: 200,
            receivedAt: new Date().toISOString(),
            provenance: {
              phase: data.run?.phase ?? "not stated",
              source_kind: data.run?.source_kind ?? "synthetic",
              authoritative: data.run?.authoritative ?? false,
              simulation_notice:
                data.run?.simulation_notice ?? "Simulation only. Not authorised for flight.",
            },
          }),
        );
      });
    };
    run();
    context.onDispose(() => {
      disposed = true;
    });
  },
};
