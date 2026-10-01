/**
 * Scenario comparison - three routing strategies side by side, and an explicit refusal to
 * over-claim what the read API does not carry.
 *
 * docs/05 section 5.6 requires the arms to share demand, weather, aircraft states, seed,
 * cutoff, energy profiles and pad/crew capacity, and warns that an intentionally weakened
 * baseline makes the result misleading. The dispatcher enforces that with
 * `WeakenedBaselineError` and `InsufficientRepetitionsError`; this panel surfaces either
 * one verbatim if it fires, and independently prints the run identity of every arm next to
 * the others so a differing seed cannot pass as the same experiment.
 *
 * The median and spread over at least ten seeds that the brief asks for is a property of
 * the dispatcher's `ComparisonReport` (simulation/scenarios/dispatch-competition-v1), and
 * /v1 does not serve it. That is stated on screen. The console will not summarise seeds it
 * was never given.
 *
 * Simulation only. Not authorised for flight.
 */

import type { Api } from "../lib/api/endpoints";
import type { Schemas } from "../lib/api/types";
import { WORLD } from "../map/world";
import type { Panel, PanelContext } from "../shell/panels";
import {
  block,
  el,
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
} from "./support/kit";
import "./panels.css";

type Run = Schemas["SimulationRunResponse"];
type Identity = Schemas["RunIdentitySchema"];

/** The three arms the scenario bundle specifies, and the run ids this server registers. */
const ARMS: readonly { strategy: string; label: string; ids: readonly string[] }[] = [
  {
    strategy: "constraint_aware_direct",
    label: "Constraint-aware direct routing",
    ids: ["chandigarh-synthetic-v1-policy-a", "run-chandigarh-synthetic-v1-policy-a"],
  },
  {
    strategy: "structured_corridor",
    label: "Structured corridor routing",
    ids: ["chandigarh-synthetic-v1-policy-b", "run-chandigarh-synthetic-v1-policy-b"],
  },
  {
    strategy: "hybrid",
    label: "Hybrid corridor plus direct",
    ids: ["chandigarh-synthetic-v1-policy-c", "run-chandigarh-synthetic-v1-policy-c"],
  },
];

const IDENTITY_ROWS: readonly { key: keyof Identity; label: string; meaning: string }[] = [
  {
    key: "seed",
    label: "seed",
    meaning: "identical seed: the demand, charge state and tie-breaks were drawn once",
  },
  {
    key: "bundle_sha256",
    label: "bundle sha256",
    meaning: "identical world: the frozen corridor and facility bundle",
  },
  {
    key: "scenario_sha256",
    label: "scenario sha256",
    meaning: "identical scenario definition, which carries the weather trace",
  },
  {
    key: "policy_version",
    label: "policy version",
    meaning: "the thing under test: arms SHOULD differ here",
  },
  {
    key: "config_release_sha256",
    label: "config release",
    meaning: "identical energy, separation and pad parameters",
  },
  { key: "engine_version", label: "engine version", meaning: "identical kernel build" },
];

interface Arm {
  readonly strategy: string;
  readonly label: string;
  readonly runId: string;
  readonly run: Run | null;
  readonly failure: string | null;
}

async function gather(api: Api): Promise<{
  arms: Arm[];
  guard: string | null;
  comparison: Schemas["ScenarioComparisonResponse"] | null;
  comparisonError: string | null;
}> {
  const arms: Arm[] = [];
  let guard: string | null = null;
  for (const arm of ARMS) {
    let run: Run | null = null;
    let failure: string | null = null;
    let used = arm.ids[0];
    for (const id of arm.ids) {
      const result = await api.simulationRun(id);
      if (result.ok) {
        run = result.data;
        used = id;
        failure = null;
        break;
      }
      failure = `${result.error.operation}: ${result.error.message}`;
      const message = `${result.error.message} ${result.error.detail ?? ""}`;
      if (/WeakenedBaseline|InsufficientRepetitions/i.test(message)) {
        guard = message;
      }
    }
    arms.push({ strategy: arm.strategy, label: arm.label, runId: used, run, failure });
  }
  const comparison = await api.compareScenarios(ARMS[0].ids[0], ARMS[1].ids[0]);
  return {
    arms,
    guard,
    comparison: comparison.ok ? comparison.data : null,
    comparisonError: comparison.ok
      ? null
      : `${comparison.error.operation}: ${comparison.error.message}`,
  };
}

/** Equality across the arms, printed per field, because "identical inputs" is a claim. */
function identityTable(arms: readonly Arm[]): HTMLElement {
  const present = arms.filter((arm) => arm.run !== null);
  return table(
    IDENTITY_ROWS.map((row) => ({
      row,
      values: present.map((arm) => String(arm.run?.run_identity[row.key] ?? "not read")),
    })),
    [
      {
        header: "input held constant?",
        cell: (entry) => entry.row.label,
        hint: (entry) => entry.row.meaning,
      },
      ...present.map(
        (arm, index): Column<{ row: (typeof IDENTITY_ROWS)[number]; values: string[] }> => ({
          header: `${arm.strategy} #${String(index + 1)}`,
          cell: (entry) => entry.values[index] ?? "not read",
        }),
      ),
      {
        header: "verdict",
        cell: (entry) => {
          const unique = new Set(entry.values);
          if (entry.row.key === "policy_version") {
            return unique.size > 1
              ? "differs, as it must: this is the arm under test"
              : "identical - so these arms are the same policy";
          }
          return unique.size === 1
            ? "identical across the arms that answered"
            : "DIFFERS: this comparison is not apples to apples";
        },
      },
    ],
    "what the arms share, field by field",
  );
}

function armCards(arms: readonly Arm[], context: PanelContext, host: HTMLElement): HTMLElement {
  const list = el("ul", { class: "cp-arms" });
  for (const arm of arms) {
    const children: Node[] = [
      el("span", { class: "cp-arm-name", text: arm.label }),
      mono(`strategy key ${arm.strategy}`),
      mono(`run id ${arm.runId}`),
    ];
    if (arm.run === null) {
      children.push(
        el("p", {
          class: "kit-state",
          text: `No run is registered under this id on this server. ${arm.failure ?? ""}`,
        }),
        el("p", {
          class: "kit-state-hint",
          text: "The scenario bundle specifies three arms. A third arm that was never run is shown as absent, not as a zero.",
        }),
      );
    } else {
      children.push(
        field("submitted", String(arm.run.submitted_orders)),
        field("accepted", String(arm.run.accepted)),
        field(
          "rejections counted",
          `${String(arm.run.rejected)} refused, and unfinished work at the cutoff is not in the contract`,
        ),
        field("on-time rate", `${(arm.run.on_time_rate * 100).toFixed(1)}%`),
        field("seed", String(arm.run.run_identity.seed)),
      );
    }
    list.append(
      block("li", { class: "cp-arm", data: { found: String(arm.run !== null) } }, children),
    );
  }
  void host;
  void context;
  return list;
}

export const comparisonPanel: Panel = {
  id: "comparison",
  slot: "comparison.runs",
  title: "Scenario comparison",
  description:
    "The three routing strategies side by side, their run identities compared field by field, rejections counted, and the seed-spread statistic named as the gap it is.",
  mount(host, context) {
    host.dataset.panelRoot = "comparison";
    let disposed = false;
    host.append(
      loadingState(
        "the comparison arms",
        "one simulation run per strategy and the two-arm comparison.",
      ),
    );
    const run = (): void => {
      void gather(context.api).then((data) => {
        if (disposed || !document.body.contains(host)) {
          return;
        }
        const read = data.arms.filter((arm) => arm.run !== null).length;
        if (read === 0) {
          host.replaceChildren(
            errorState(
              {
                kind: "http",
                operation: "getSimulationRun",
                message: `no comparison arm answered: ${data.arms.map((arm) => arm.failure ?? "").join(" | ")}`,
              },
              run,
            ),
          );
          return;
        }
        host.replaceChildren();
        host.append(
          paragraph(
            "Rejections are counted, not filtered out: an arm that served less is not an arm that failed less. Every figure below describes one frozen synthetic scenario, and the ranking is inside this simulation only.",
          ),
        );
        if (data.guard !== null) {
          host.append(
            block("div", { class: "cp-guard", role: "alert" }, [
              el("h4", {
                class: "kit-state-heading",
                text: "The dispatcher's baseline guard fired",
              }),
              mono(data.guard),
              paragraph(
                "The comparison was refused because an arm did not receive identical inputs, or because too few seeds were run. That refusal is the result: a weakened baseline is not a win, and the console does not draw a chart over it.",
              ),
            ]),
          );
        }
        host.append(
          sectionTitle(`Arms (${read} of ${ARMS.length} answered)`),
          armCards(data.arms, context, host),
          sectionTitle("Identical inputs, checked on screen"),
          identityTable(data.arms),
          fieldList(
            [
              field(
                "demand, weather, fleet, cutoff",
                "held constant per simulation/scenarios/dispatch-competition-v1/comparison.yaml - the read API does not serve that document, so the console prints its name rather than copying its numbers",
              ),
              field(
                "median and spread over >=10 seeds",
                "not exposed by /v1. compareScenarios returns two runs and one verdict string; the dispatcher's ComparisonReport holds the seed summaries and no read operation carries them",
              ),
              field(
                "rejections",
                "counted per arm above from getSimulationRun.rejected; unfinished-at-cutoff work is a separate bucket the contract does not carry",
              ),
              field(
                "scenario",
                `${WORLD.provenance.scenario_id}, seed ${String(WORLD.provenance.seed)}`,
              ),
            ],
            "what this screen can and cannot claim",
          ),
        );
        if (data.comparison !== null) {
          const verdict = data.comparison;
          host.append(
            sectionTitle("The two-arm comparison the API does serve"),
            fieldList(
              [
                field(
                  "verdict",
                  verdict.verdict,
                  "the server's own words; the console adds no recommendation",
                ),
                field(
                  "scenario / against",
                  `${verdict.scenario_id ?? "not stated"} vs ${verdict.against ?? "not stated"}`,
                ),
                ...Object.entries(verdict.compared_metrics ?? {}).map(([key, value]) =>
                  field(`metric ${key}`, value),
                ),
              ],
              "comparison payload as returned",
            ),
          );
        }
        if (data.comparisonError !== null) {
          host.append(
            el("p", {
              class: "dt-partials",
              role: "status",
              text: `compareScenarios: ${data.comparisonError}`,
            }),
          );
        }
        host.append(
          provenanceLine(context, {
            transportMode: context.api.transport.mode,
            transportLabel: context.api.transport.label,
            operation: "getSimulationRun",
            status: 200,
            receivedAt: new Date().toISOString(),
            provenance: {
              phase: data.arms[0]?.run?.phase ?? "not stated",
              source_kind: "synthetic",
              authoritative: false,
              simulation_notice: "Simulation only. Not authorised for flight.",
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
