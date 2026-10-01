/**
 * Fleet view - and the sentence the demo brief insists on: "Connected" is not
 * availability (docs/05-chandigarh-demo.md 5.4).
 *
 * Two independent channels are therefore rendered side by side and never merged:
 *
 *   - the ledger's operational state (`AircraftResponse.status`: available, reserved,
 *     loading, flying, unloading, charging, maintenance, offline, contingency...), and
 *   - the link (`link_quality`: good, degraded, offline), which says only that a
 *     telemetry path exists.
 *
 * An aircraft that is online but not available is listed with the reason it is not
 * available, taken from its own status and from the reservation ledger rows that hold it.
 * An aircraft marked available with no link is called out the other way: the ledger would
 * hand it work it cannot be told to fly. The console reports the mismatch; it does not
 * resolve it, because resolving it is the dispatcher's job (docs/conventions.md §7).
 *
 * Simulation only. Not authorised for flight.
 */

import type { ApiError, ResponseMeta } from "../lib/api/client";
import type { Schemas } from "../lib/api/types";
import type { Panel, PanelContext } from "../shell/panels";
import {
  block,
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
  shortId,
  table,
  type Column,
} from "./support/kit";
import "./panels.css";

type Aircraft = Schemas["AircraftResponse"];
type Facility = Schemas["FacilityResponse"];
type Capacity = Schemas["FacilityCapacityResponse"];
type Reservation = Schemas["ReservationResponse"];

/** The frozen run's own horizon - the same window the map and the shell ask for. */
const WINDOW_FROM = "2026-10-01T09:00:00Z";
const WINDOW_TO = "2026-10-01T10:00:00Z";

/** Operational states the fleet view groups by, in the brief's order. */
const GROUPS: readonly { key: string; label: string; meaning: string }[] = [
  { key: "available", label: "available", meaning: "the ledger would offer this tail for work" },
  {
    key: "reserved",
    label: "reserved",
    meaning: "held by a commitment; not free even if reachable",
  },
  { key: "loading", label: "loading", meaning: "at the pad with payload being attached" },
  { key: "flying", label: "flying", meaning: "airborne on a committed plan" },
  { key: "unloading", label: "unloading", meaning: "arrived, handover not finished" },
  {
    key: "charging",
    label: "charging",
    meaning: "on a charger bay; not available until the reserve is met",
  },
  { key: "maintenance", label: "maintenance", meaning: "airworthiness blocked; never eligible" },
  { key: "offline", label: "offline", meaning: "no link and no ledger availability" },
  {
    key: "contingency",
    label: "contingency",
    meaning: "in a contingency state; capacity is frozen around it",
  },
  { key: "registered", label: "registered", meaning: "known to the fleet, not yet in service" },
  { key: "retired", label: "retired", meaning: "out of service for good" },
];

interface FleetData {
  readonly aircraft: readonly Aircraft[];
  readonly facilities: readonly Facility[];
  readonly capacities: readonly { facility: Facility; capacity: Capacity | null }[];
  readonly reservations: readonly Reservation[];
  readonly meta: ResponseMeta;
  readonly failures: readonly string[];
}

async function gather(
  api: PanelContext["api"],
): Promise<{ ok: true; data: FleetData } | { ok: false; error: ApiError }> {
  const aircraftResult = await api.aircraft();
  if (!aircraftResult.ok) {
    return { ok: false, error: aircraftResult.error };
  }
  const [facilitiesResult, reservationsResult] = await Promise.all([
    api.facilities(),
    api.reservations(WINDOW_FROM, WINDOW_TO),
  ]);
  const facilities: readonly Facility[] = facilitiesResult.ok ? facilitiesResult.data.items : [];
  const hubs = facilities.filter((facility) => facility.kind === "hub").slice(0, 6);
  const capacities = await Promise.all(
    hubs.map(async (facility) => {
      const result = await api.facilityCapacity(facility.facility_id);
      return { facility, capacity: result.ok ? result.data : null };
    }),
  );
  const failures: string[] = [];
  if (!facilitiesResult.ok) {
    failures.push(`facilities: ${facilitiesResult.error.message}`);
  }
  if (!reservationsResult.ok) {
    failures.push(`reservations: ${reservationsResult.error.message}`);
  }
  for (const entry of capacities) {
    if (entry.capacity === null) {
      failures.push(`capacity for ${entry.facility.facility_id}: not answered`);
    }
  }
  return {
    ok: true,
    data: {
      aircraft: aircraftResult.data.items,
      facilities,
      capacities,
      reservations: reservationsResult.ok ? reservationsResult.data.items : [],
      meta: aircraftResult.meta,
      failures,
    },
  };
}

/** Which operational states actually exist in the fleet, plus anything unexpected. */
function groupByStatus(aircraft: readonly Aircraft[]): Map<string, Aircraft[]> {
  const groups = new Map<string, Aircraft[]>();
  for (const entry of GROUPS) {
    groups.set(entry.key, []);
  }
  for (const tail of aircraft) {
    const bucket = groups.get(tail.status);
    if (bucket === undefined) {
      groups.set(tail.status, [tail]);
    } else {
      bucket.push(tail);
    }
  }
  return groups;
}

function isOnline(link: string | undefined): boolean {
  return link === "good" || link === "degraded";
}

function onlineButUnavailable(aircraft: readonly Aircraft[]): Aircraft[] {
  return aircraft.filter((tail) => isOnline(tail.link_quality) && tail.status !== "available");
}

function availableButUnreachable(aircraft: readonly Aircraft[]): Aircraft[] {
  return aircraft.filter((tail) => tail.status === "available" && !isOnline(tail.link_quality));
}

const AIRCRAFT_COLUMNS: readonly Column<Aircraft>[] = [
  {
    header: "tail",
    cell: (row) => row.registration_ref,
    hint: (row) => `aircraft_id ${row.aircraft_id}; model ${row.model_id}`,
  },
  { header: "ledger state", cell: (row) => row.status },
  { header: "link", cell: (row) => row.link_quality ?? "not reported" },
  { header: "base", cell: (row) => row.base_facility_id ?? "no base facility" },
  {
    header: "available?",
    cell: (row) => (row.status === "available" ? "yes - per the ledger" : `no - ${row.status}`),
    hint: () => "this column reads status only. A good link never makes an aircraft available.",
  },
  {
    header: "provenance",
    cell: (row) => `${row.source_kind} · authoritative ${String(row.authoritative)}`,
  },
];

function countsBlock(groups: Map<string, Aircraft[]>): HTMLElement {
  const items = el("ul", { class: "fl-counts", aria: { label: "aircraft by operational state" } });
  for (const entry of GROUPS) {
    const count = groups.get(entry.key)?.length ?? 0;
    items.append(
      block("li", { class: "fl-count", title: entry.meaning }, [
        el("span", { class: "fl-count-value", text: String(count) }),
        el("span", { class: "fl-count-key", text: entry.label }),
      ]),
    );
  }
  return block("div", { class: "fl-availability" }, [
    el("h4", {
      class: "fl-availability-heading",
      text: "Availability is a ledger state, not a connection",
    }),
    paragraph(
      "Counted from AircraftResponse.status. Link quality is reported in its own column and contributes nothing to these counts, because an online aircraft that is charging cannot be dispatched.",
    ),
    items,
  ]);
}

function confusionBlock(aircraft: readonly Aircraft[]): HTMLElement {
  const online = onlineButUnavailable(aircraft);
  const unreachable = availableButUnreachable(aircraft);
  const items = el("ul", { class: "fl-confusions" });
  for (const tail of online) {
    const held = tail.status;
    items.append(
      block("li", { class: "fl-confusion" }, [
        el("strong", {
          text: `${tail.registration_ref}: connected (${tail.link_quality ?? "link"}), not available`,
        }),
        paragraph(
          `Why they differ: the ledger holds it as "${held}". A good link is a telemetry path, not a free aircraft${
            held === "charging" ? " - it is on a charger bay" : ""
          }${held === "flying" ? " - it is airborne on a committed reservation" : ""}${
            held === "maintenance"
              ? " - airworthiness is blocked, and a maintenance tail is never eligible"
              : ""
          }.`,
        ),
        mono(
          `aircraft ${shortId(tail.aircraft_id, 6)} · base ${tail.base_facility_id ?? "not stated"}`,
        ),
      ]),
    );
  }
  for (const tail of unreachable) {
    items.append(
      block("li", { class: "fl-confusion" }, [
        el("strong", {
          text: `${tail.registration_ref}: available on the ledger, not reachable (${tail.link_quality ?? "no link reported"})`,
        }),
        paragraph(
          "The other direction matters just as much: the ledger would offer this tail for work, but with no live link its position is not known and its protected volume is not current. This is the state that freezes capacity around an uncertain aircraft.",
        ),
        mono(`aircraft ${shortId(tail.aircraft_id, 6)}`),
      ]),
    );
  }
  if (items.children.length === 0) {
    items.append(
      el("li", {
        class: "fl-confusion",
        text: "No aircraft disagrees with its own link right now: every connected tail is also doing something, and every available tail is reachable.",
      }),
    );
  }
  return block("div", { class: "fl-confusions-wrap" }, [
    sectionTitle(`Where "connected" and "available" disagree (${items.children.length})`),
    items,
  ]);
}

function facilityBlock(data: FleetData): HTMLElement {
  const outages = data.facilities.filter(
    (facility) =>
      facility.status.toLowerCase() !== "active" && facility.status.toLowerCase() !== "operational",
  );
  const rows: Node[] = [
    paragraph(
      `${data.facilities.length} facilities in the frozen bundle. ${outages.length} are not reporting active - a closed pad is capacity that has disappeared, and the queue absorbs it as a pad-busy reason rather than as a failure.`,
    ),
    table(
      data.capacities,
      [
        {
          header: "facility",
          cell: (row) => `${row.facility.name} - ${row.facility.facility_id}`,
          hint: () =>
            "fictional name from the synthetic bundle; the id is the node the ledger uses",
        },
        { header: "status", cell: (row) => row.facility.status },
        { header: "outage reason", cell: (row) => row.facility.outage_reason ?? "none recorded" },
        {
          header: "pads",
          cell: (row) =>
            row.capacity === null
              ? "not answered"
              : `${row.capacity.pads_committed ?? 0} of ${row.capacity.pad_capacity} committed`,
        },
        {
          header: "charger bays",
          cell: (row) =>
            row.capacity === null
              ? "not answered"
              : `${row.capacity.chargers_committed ?? 0} of ${row.capacity.charger_capacity} committed`,
        },
        {
          header: "handover",
          cell: (row) => (row.capacity?.handover_modes ?? []).join(", ") || "not stated",
        },
      ] as readonly Column<FleetData["capacities"][number]>[],
      "pad and charger capacity in the run window",
    ),
  ];
  return block("div", { class: "fl-facilities" }, [
    sectionTitle("Pads, chargers and crew"),
    ...rows,
  ]);
}

function ledgerBlock(data: FleetData): HTMLElement {
  const held = data.reservations.filter(
    (row) => row.state === "committed" || row.state === "unresolved",
  );
  if (held.length === 0) {
    return block("div", { class: "fl-held" }, [
      sectionTitle("What the ledger holds right now"),
      emptyState(
        "capacity held by the ledger",
        `No committed or unresolved reservation exists in ${WINDOW_FROM} to ${WINDOW_TO}. Nothing is frozen by a hold - and nothing is protected either.`,
        "a committed corridor, pad, charger bay or crew reservation in the window.",
      ),
    ]);
  }
  return block("div", { class: "fl-held" }, [
    sectionTitle(`Capacity held by the ledger in the run window (${held.length})`),
    paragraph(
      "This is the other half of availability: a pad or charger bay that is committed is not free regardless of what the aircraft list says.",
    ),
    table(
      held,
      [
        { header: "resource", cell: (row) => row.resource_key },
        { header: "kind", cell: (row) => row.resource_kind },
        { header: "state", cell: (row) => row.state },
        { header: "mission", cell: (row) => shortId(row.mission_id) },
      ] as readonly Column<Reservation>[],
      "committed and unresolved reservations",
    ),
  ]);
}

function render(host: HTMLElement, context: PanelContext, data: FleetData): void {
  const groups = groupByStatus(data.aircraft);
  host.replaceChildren();
  host.append(
    paragraph(
      "One operator controls every tail in this scenario (docs/05 section 5.10). The energy figure this screen cannot show is a contract gap, not a rendering choice: AircraftResponse carries status, link quality and a base facility, and no charge value - energy margins appear on plan versions and in the mission queue instead.",
    ),
    countsBlock(groups),
    confusionBlock(data.aircraft),
    table(data.aircraft, AIRCRAFT_COLUMNS, "every aircraft the fleet register holds"),
    facilityBlock(data),
    ledgerBlock(data),
  );
  if (data.failures.length > 0) {
    host.append(
      field(
        "partial reads",
        data.failures.join(" | "),
        "the blocks above print as unanswered rather than assuming a value",
      ),
    );
  }
  host.append(
    fieldList(
      [field("fleet count", `${data.aircraft.length} tails, ${data.facilities.length} facilities`)],
      "scope",
    ),
    provenanceLine(context, data.meta),
  );
}

export const fleetPanel: Panel = {
  id: "fleet",
  slot: "fleet.status",
  title: "Fleet, pads and chargers",
  description:
    "Availability, charging, flying, maintenance and offline - with the link reported separately, because connected is not available.",
  mount(host, context) {
    host.dataset.panelRoot = "fleet";
    let disposed = false;
    host.append(
      loadingState("the fleet register", "then the facility list and the capacity of each hub."),
    );
    const run = (): void => {
      void gather(context.api).then((result) => {
        if (disposed || !document.body.contains(host)) {
          return;
        }
        if (!result.ok) {
          host.replaceChildren(errorState(result.error, run));
          return;
        }
        if (result.data.aircraft.length === 0) {
          host.replaceChildren(
            emptyState(
              "the fleet register",
              "The API answered with no aircraft at all. A fleet this empty serves nothing, so every order in the queue is unserved for the same reason.",
              "provisioned tails in the simulation deployment - apps/sim-runner seeds them.",
            ),
          );
          return;
        }
        render(host, context, result.data);
      });
    };
    run();
    context.onDispose(() => {
      disposed = true;
    });
  },
};
