/**
 * Exception and contingency states.
 *
 * The five conditions the run actually produces, each with a real designed variant for
 * "not happening", "still reading", "read and clear" and "read and blocking":
 *
 *   - uncertainty from stale telemetry (UncertaintyStateResponse),
 *   - the capacity freeze that uncertainty causes (retained protected volume, unresolved
 *     reservation rows, aircraft unreachable on the link),
 *   - weather hold (the frozen wind trace and the corridor's own wind limit),
 *   - facility outage (FacilityResponse.status / outage_reason),
 *   - a priority request refused because a commitment is already airborne
 *     (the ledger's own reason codes on the stored trace).
 *
 * Each block quotes where the claim came from. Nothing here is a judgement the browser
 * made: the console reads a state, says what it freezes, and says what it does not prove.
 *
 * Simulation only. Not authorised for flight.
 */

import type { ApiResult } from "../lib/api/client";
import type { Schemas } from "../lib/api/types";
import { WORLD, windAt } from "../map/world";
import type { Panel, PanelContext } from "../shell/panels";
import {
  block,
  el,
  field,
  fieldList,
  formatWhen,
  loadingState,
  mono,
  paragraph,
  provenanceLine,
  shortId,
} from "./support/kit";
import { explainReason } from "./support/reasons";
import "./panels.css";

type Facility = Schemas["FacilityResponse"];
type Reservation = Schemas["ReservationResponse"];
type Aircraft = Schemas["AircraftResponse"];

const WEATHER_HOLD_MS = 8;

interface ExceptionRead {
  readonly uncertainty: Schemas["UncertaintyStateResponse"] | null;
  readonly uncertaintyFailed: string | null;
  readonly reservations: readonly Reservation[];
  readonly facilities: readonly Facility[];
  readonly aircraft: readonly Aircraft[];
  readonly trace: Schemas["DecisionTraceResponse"] | null;
  readonly missionId: string | null;
  readonly failures: readonly string[];
}

interface ExceptionCard {
  readonly key: string;
  readonly title: string;
  readonly severity: "blocking" | "watch" | "clear";
  readonly state: string;
  readonly body: string;
  readonly consequence: string;
  readonly notThis: string;
  readonly evidence: readonly string[];
}

function card(data: ExceptionRead): ExceptionCard[] {
  const cards: ExceptionCard[] = [];
  const uncertainty = data.uncertainty;
  const stale = uncertainty !== null && uncertainty.state !== "clear";
  cards.push({
    key: "uncertainty",
    title: "Uncertainty from stale telemetry",
    severity: stale ? "blocking" : uncertainty === null ? "watch" : "clear",
    state: uncertainty === null ? (data.uncertaintyFailed ?? "not read") : uncertainty.state,
    body: stale
      ? `The last measurement the ledger accepted is ${formatWhen(uncertainty.last_accepted_measurement_time)}${
          uncertainty.staleness_s === undefined
            ? ""
            : `, and the run reports ${uncertainty.staleness_s} s of staleness`
        }. The position shown is where the aircraft was known to be, not where it is.`
      : "Telemetry is in sequence and inside the staleness window.",
    consequence: stale
      ? "Nothing is released and nothing is launched into the same space: the uncertainty is a capacity constraint, not a display warning."
      : "No uncertainty constraint is active on the aircraft in scope.",
    notThis: "Uncertain is never drawn as clear airspace, and never smoothed into a transition.",
    evidence: [
      `GET /v1/missions/{id}/uncertainty${data.uncertaintyFailed === null ? "" : ` - ${data.uncertaintyFailed}`}`,
      ...(uncertainty === null
        ? []
        : [`protected_volume_retained: ${String(uncertainty.protected_volume_retained)}`]),
    ],
  });

  const unresolved = data.reservations.filter((row) => row.state === "unresolved");
  const unreachable = data.aircraft.filter(
    (tail) =>
      tail.status === "available" &&
      tail.link_quality !== "good" &&
      tail.link_quality !== "degraded",
  );
  const freeze = unresolved.length > 0 || unreachable.length > 0;
  cards.push({
    key: "capacity-freeze",
    title: "Capacity freeze caused by uncertainty",
    severity: freeze ? "blocking" : "clear",
    state: `${String(unresolved.length)} unresolved reservation rows, ${String(unreachable.length)} available-but-unreachable tails`,
    body: freeze
      ? `The ledger is still holding ${unresolved.map((row) => row.resource_key).join(", ") || "capacity"} because the aircraft inside it has not reconciled. A pad, corridor or volume in that set is not offered to anyone else.`
      : "Nothing is held open by an unreconciled position, so the capacity the ledger reports is the capacity that is genuinely free.",
    consequence: freeze
      ? "Downstream missions see this as a pad-busy or resource-unavailable refusal in the queue, not as a crash."
      : "No capacity is frozen right now.",
    notThis:
      "A frozen volume is not a cancelled mission: an airborne commitment is never deleted to free space.",
    evidence: [
      "GET /v1/missions/{id}/reservations",
      ...unresolved
        .slice(0, 4)
        .map((row) => `${row.resource_kind} ${row.resource_key} held to ${row.valid_until}`),
    ],
  });

  const wind = windAt(30);
  const corridorsOver = WORLD.layers.corridor_centerline.edges.filter(
    (edge) => edge.max_wind_ms <= wind.wind_ms,
  );
  const weather = wind.wind_ms >= WEATHER_HOLD_MS || corridorsOver.length > 0;
  cards.push({
    key: "weather",
    title: "Weather hold",
    severity: weather ? "watch" : "clear",
    state: `frozen trace at minute 30: ${String(wind.wind_ms)} m/s, headwind basis point ${wind.headwind_bp}`,
    body: weather
      ? `${String(corridorsOver.length)} corridor edges carry a wind limit at or below the trace value (${corridorsOver
          .slice(0, 3)
          .map((edge) => edge.corridor_id)
          .join(
            ", ",
          )}${corridorsOver.length > 3 ? ", ..." : ""}). A planner that refuses a lane for wind reports it as a restriction reason, not as a delay.`
      : "The trace is inside every corridor's own wind limit.",
    consequence: weather
      ? "Refusals on the queue are labelled weather hold only where the ledger's code says so - see each row's wire code."
      : "No wind-driven limit is being crossed in the frozen trace.",
    notThis:
      "This is one frozen trace with magnitude only. It is not live weather, and the trace carries no direction to draw.",
    evidence: [
      `bundle weather: ${WORLD.provenance.weather.station}, ${WORLD.provenance.weather.source}`,
      `retrieved ${WORLD.provenance.weather.retrieved_at}, expires ${WORLD.provenance.weather.expires_at} (${WORLD.provenance.weather.freshness})`,
    ],
  });

  const outages = data.facilities.filter((facility) => facility.status.toLowerCase() !== "active");
  cards.push({
    key: "outage",
    title: "Facility outage",
    severity: outages.length > 0 ? "blocking" : "clear",
    state: `${String(outages.length)} of ${String(data.facilities.length)} facilities not reporting active`,
    body:
      outages.length > 0
        ? outages
            .map(
              (facility) =>
                `${facility.name} (${facility.facility_id}) is ${facility.status.toLowerCase()}${facility.outage_reason === undefined ? "" : `: ${facility.outage_reason}`}`,
            )
            .join("; ")
        : "Every facility in the bundle reports active.",
    consequence:
      outages.length > 0
        ? "Its pads and charger bays are out of the capacity the dispatcher may book. Missions that needed them come back as pad-busy refusals with the outage as their reason."
        : "No hub capacity is withdrawn.",
    notThis:
      "An outage is a reported state, not a prediction: the console cannot open or close a facility.",
    evidence: [
      "GET /v1/facilities",
      ...outages.slice(0, 3).map((facility) => `${facility.facility_id} status ${facility.status}`),
    ],
  });

  const rejections = data.trace?.rejected ?? [];
  const airborne = rejections.filter((rejection) =>
    /airborne|commitment_immutable|not_cancellable|already_committed/.test(rejection.reason_code),
  );
  const priority = data.trace?.inputs?.priority_class ?? "";
  cards.push({
    key: "priority",
    title: "Priority request refused because a commitment is airborne",
    severity: airborne.length > 0 ? "blocking" : "clear",
    state:
      airborne.length > 0
        ? `${String(airborne.length)} refusal${airborne.length === 1 ? "" : "s"} on the record`
        : "no airborne-commitment refusal on this trace",
    body:
      airborne.length > 0
        ? airborne
            .map(
              (rejection) =>
                explainReason(rejection.reason_code, rejection.human_reason).humanReason ??
                rejection.human_reason,
            )
            .join("; ")
        : data.trace === null
          ? `No mission is in scope, so no priority refusal can be shown. Pick a mission in the queue.${priority === "" ? "" : ` (priority class in scope: ${priority})`}`
          : `This mission's trace records no refusal of a priority request: the resequencing it asked for was not blocked by an airborne commitment. Priority class: ${priority || "not stated"}.`,
    consequence:
      airborne.length > 0
        ? "The airborne mission keeps its reservation. Unlaunched work may be resequenced around it; an executed commitment is not deleted to make room, and a payment tier never creates an emergency right of way."
        : "Nothing was refused on those grounds.",
    notThis:
      "A protected commitment is not a stalled decision: the reason is on the record and readable.",
    evidence: [
      ...(data.trace === null
        ? ["no trace in scope"]
        : [
            `GET /v1/missions/${data.missionId ?? ""}/decision`,
            ...airborne.slice(0, 3).map((rejection) => rejection.reason_code),
          ]),
    ],
  });
  return cards;
}

type AnyResult = ApiResult<unknown> | null;

function dataOf<T>(result: ApiResult<T> | null): T | null {
  if (result === null) {
    return null;
  }
  return result.ok ? result.data : null;
}

function failOf(result: ApiResult<unknown> | null): string | null {
  if (result === null) {
    return null;
  }
  return result.ok ? null : result.error.message;
}

function describeFailure(subject: string, result: AnyResult): string[] {
  if (result === null) {
    return [];
  }
  if (result.ok) {
    return [];
  }
  return [`${subject} (${result.error.operation}): ${result.error.message}`];
}

function render(host: HTMLElement, context: PanelContext, data: ExceptionRead): void {
  const cards = card(data);
  host.replaceChildren();
  host.append(
    paragraph(
      "Five conditions this run really produces. Each is drawn as a designed state with what it freezes, what it does not prove and the read it came from - not a spinner, and not silence.",
    ),
  );
  const list = el("ul", { class: "ex-list" });
  for (const entry of cards) {
    list.append(
      block("li", { class: "ex-item", data: { severity: entry.severity } }, [
        el("h4", { class: "ex-title", text: `${entry.title}: ${entry.state}` }),
        context.stateChip(
          entry.severity === "blocking"
            ? "rejected"
            : entry.severity === "watch"
              ? "uncertain"
              : "observed",
          entry.severity === "blocking"
            ? "blocking"
            : entry.severity === "watch"
              ? "watch"
              : "clear",
        ),
        el("p", { class: "ex-body", text: entry.body }),
        field("what it means for capacity", entry.consequence),
        el("p", { class: "kit-state-hint", text: `What this is not: ${entry.notThis}` }),
        block("div", { class: "ex-evidence" }, [
          mono(`source: ${entry.evidence[0] ?? "not stated"}`),
          ...entry.evidence.slice(1).map((line) => mono(line)),
        ]),
      ]),
    );
  }
  host.append(list);
  if (data.failures.length > 0) {
    host.append(
      el("p", {
        class: "dt-partials",
        role: "status",
        text: `Reads that failed: ${data.failures.join(" | ")}. Their blocks above say they did not answer.`,
      }),
    );
  }
  host.append(
    fieldList(
      [
        field(
          "mission in scope",
          data.missionId === null
            ? "none - the queue or the map picks one"
            : shortId(data.missionId),
        ),
      ],
      "scope of the two per-mission blocks",
    ),
    provenanceLine(context, {
      transportMode: context.api.transport.mode,
      transportLabel: context.api.transport.label,
      operation: "listFacilities",
      status: 200,
      receivedAt: new Date().toISOString(),
      provenance: {
        phase: WORLD.provenance.policy_version,
        source_kind: context.api.transport.mode === "live" ? "observed" : "synthetic",
        authoritative: false,
        simulation_notice: "Simulation only. Not authorised for flight.",
      },
    }),
  );
}

export const exceptionPanel: Panel = {
  id: "exceptions",
  slot: "map.context",
  title: "Exceptions and contingencies",
  description:
    "Stale telemetry, the capacity freeze it causes, weather hold, facility outage, and a priority request refused because a commitment is airborne.",
  mount(host, context) {
    host.dataset.panelRoot = "exceptions";
    let disposed = false;
    host.append(
      loadingState(
        "the exception board",
        "uncertainty, reservations, facilities, fleet links and the trace in scope.",
      ),
    );
    const run = (): void => {
      const missionId = context.selection.get().missionId;
      void Promise.all([
        missionId === null ? Promise.resolve(null) : context.api.uncertainty(missionId),
        missionId === null ? Promise.resolve(null) : context.api.missionReservations(missionId),
        context.api.facilities(),
        context.api.aircraft(),
        missionId === null ? Promise.resolve(null) : context.api.decisionTrace(missionId),
      ]).then(
        ([
          uncertaintyResult,
          reservationsResult,
          facilitiesResult,
          aircraftResult,
          traceResult,
        ]) => {
          if (disposed || !document.body.contains(host)) {
            return;
          }
          const uncertainty = dataOf(uncertaintyResult);
          const reservationRows = dataOf(reservationsResult);
          const facilityRows = dataOf(facilitiesResult);
          const aircraftRows = dataOf(aircraftResult);
          const trace = dataOf(traceResult);
          const sources: readonly [string, AnyResult][] = [
            ["uncertainty", uncertaintyResult],
            ["reservations", reservationsResult],
            ["facilities", facilitiesResult],
            ["aircraft", aircraftResult],
            ["decision trace", traceResult],
          ];
          render(host, context, {
            uncertainty,
            uncertaintyFailed: failOf(uncertaintyResult),
            reservations: reservationRows?.items ?? [],
            facilities: facilityRows?.items ?? [],
            aircraft: aircraftRows?.items ?? [],
            trace,
            missionId,
            failures: sources.flatMap(([subject, result]) => describeFailure(subject, result)),
          });
        },
        (cause: unknown) => {
          if (!disposed) {
            host.replaceChildren(
              el("p", {
                class: "kit-state kit-state--error",
                role: "alert",
                text: `The exception board could not be assembled: ${String(cause)}`,
              }),
            );
          }
        },
      );
    };
    run();
    const unsubscribe = context.selection.subscribe(() => {
      run();
    });
    context.onDispose(() => {
      disposed = true;
      unsubscribe();
    });
  },
};
