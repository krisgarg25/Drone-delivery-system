/**
 * Typed client for the DroneOps contract.
 *
 * Two transports share one interface:
 *   - `createHttpTransport()`  - real `/v1/...` calls through the Vite/nginx proxy.
 *   - `createSyntheticTransport()` - dev fixtures, clearly labelled, see `mock.ts`.
 *
 * Every result carries `meta.transportMode`. Nothing in the UI is allowed to render a
 * payload without consulting it, which is what makes the synthetic transport impossible
 * to mistake for live data: a synthetic payload is drawn in the synthetic style
 * (dash-dot, diamond, `SYNTH` tag) and the provenance strip names the transport.
 *
 * The client exposes READ operations only. The nine POST operations in the contract
 * (order intake, plan, reserve, cancel, proposal accept/decline, facility outage) mutate
 * server-side workflow state and are deliberately not reachable from this module: the
 * console presents, it never decides (docs/conventions.md section 7, and the ESLint
 * no-restricted-syntax rule that enforces it).
 *
 * Simulation only. Not authorised for flight.
 */

import type { DataState, OperationId, Provenance, ResponseFor } from "./types";
import { provenanceOf } from "./types";

export const SIMULATION_NOTICE = "Simulation only. Not authorised for flight.";

/** The read-only half of the contract, with the path template for each operation. */
export const READ_ROUTES = {
  listOrders: "/v1/orders",
  getOrder: "/v1/orders/{id}",
  listMissionPlanVersions: "/v1/missions/{id}/plan-versions",
  getMissionDecisionTrace: "/v1/missions/{id}/decision",
  getMissionUncertaintyState: "/v1/missions/{id}/uncertainty",
  listMissionReservations: "/v1/missions/{id}/reservations",
  listMissionObservations: "/v1/missions/{id}/observations",
  listAircraft: "/v1/aircraft",
  getAircraft: "/v1/aircraft/{id}",
  listFacilities: "/v1/facilities",
  getFacilityCapacity: "/v1/facilities/{id}/capacity",
  listReservations: "/v1/reservations",
  listAuditEvents: "/v1/audit-events",
  getSimulationRun: "/v1/simulations/{run_id}",
  getSimulationReplayDiff: "/v1/simulations/{run_id}/replay-diff",
  compareScenarios: "/v1/scenarios/{scenario_id}/compare",
} as const satisfies Partial<Record<OperationId, string>>;

/** Writing is a server-side act; the console has no route to it. */
export const MUTATION_OPERATIONS: readonly OperationId[] = [
  "createOrder",
  "cancelOrder",
  "requestMissionPlan",
  "reserveMissionResources",
  "cancelMission",
  "previewMissionProposal",
  "acceptMissionProposal",
  "declineMissionProposal",
  "reportFacilityOutage",
];

export type ReadOperationId = keyof typeof READ_ROUTES & OperationId;

export type TransportMode = "live" | "synthetic";

export interface CallArgs {
  path?: Record<string, string>;
  query?: Record<string, string | undefined>;
  signal?: AbortSignal;
}

export interface ResponseMeta {
  readonly transportMode: TransportMode;
  readonly transportLabel: string;
  readonly operation: ReadOperationId;
  readonly status: number;
  readonly receivedAt: string;
  readonly provenance: Provenance | null;
}

export type ApiErrorKind = "network" | "http" | "contract" | "aborted";

export interface ApiError {
  readonly kind: ApiErrorKind;
  readonly message: string;
  readonly status?: number;
  readonly operation: ReadOperationId;
  readonly detail?: string;
}

export type ApiResult<T> =
  | { readonly ok: true; readonly data: T; readonly meta: ResponseMeta }
  | {
      readonly ok: false;
      readonly error: ApiError;
    };

export interface Transport {
  readonly mode: TransportMode;
  readonly label: string;
  get<Id extends ReadOperationId>(id: Id, args?: CallArgs): Promise<ApiResult<ResponseFor<Id>>>;
}

/* ------------------------------------------------------------------ http transport */

/**
 * The console authenticates as the simulation deployment's READ-ONLY observer credential
 * (`sim-observer-a`, apps/api/src/droneops_api/composition.py). Override with
 * VITE_DRONEOPS_TOKEN; there is no writable console credential because the console has no
 * writable operations.
 */
export const READ_ONLY_TOKEN = "sim-observer-a";

export function bearerToken(): string {
  // vite/client types import.meta.env as an open `any` record; the console reads exactly
  // one key, so it is narrowed here rather than handled as `any` at each call site.
  const env: Record<string, string | undefined> = import.meta.env as unknown as Record<
    string,
    string | undefined
  >;
  return env.VITE_DRONEOPS_TOKEN ?? READ_ONLY_TOKEN;
}

export interface HttpTransportOptions {
  readonly baseUrl?: string;
  readonly fetchImpl?: typeof fetch;
  readonly timeoutMs?: number;
}

export function createHttpTransport(options: HttpTransportOptions = {}): Transport {
  const baseUrl = options.baseUrl ?? "";
  const timeoutMs = options.timeoutMs ?? 5000;
  const label = "live API over the dev proxy";

  return {
    mode: "live",
    label,
    async get<Id extends ReadOperationId>(
      id: Id,
      args: CallArgs = {},
    ): Promise<ApiResult<ResponseFor<Id>>> {
      const url = buildUrl(baseUrl, READ_ROUTES[id], args);
      const controller = new AbortController();
      const timer = setTimeout(() => {
        controller.abort(new DOMException("timeout", "TimeoutError"));
      }, timeoutMs);
      if (args.signal) {
        args.signal.addEventListener(
          "abort",
          () => {
            controller.abort(args.signal?.reason);
          },
          {
            once: true,
          },
        );
      }
      try {
        const response = await (options.fetchImpl ?? fetch)(url, {
          method: "GET",
          headers: { accept: "application/json", authorization: `Bearer ${bearerToken()}` },
          signal: controller.signal,
        });
        const body: unknown = await readJson(response);
        if (!response.ok) {
          return {
            ok: false,
            error: {
              kind: "http",
              status: response.status,
              message: describeHttpError(body) ?? `HTTP ${response.status}`,
              operation: id,
              detail: url,
            },
          };
        }
        const provenance = provenanceOf((body ?? {}) as object);
        return {
          ok: true,
          data: body as ResponseFor<Id>,
          meta: {
            transportMode: "live",
            transportLabel: label,
            operation: id,
            status: response.status,
            receivedAt: new Date().toISOString(),
            provenance,
          },
        };
      } catch (cause) {
        const aborted = cause instanceof DOMException && cause.name === "AbortError";
        return {
          ok: false,
          error: {
            kind: aborted ? "aborted" : "network",
            message: aborted
              ? "request cancelled"
              : `API unreachable - start it with \`make api\` (${String(cause)})`,
            operation: id,
            detail: url,
          },
        };
      } finally {
        clearTimeout(timer);
      }
    },
  };
}

async function readJson(response: Response): Promise<unknown> {
  const text = await response.text();
  if (text === "") {
    return null;
  }
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return text;
  }
}

function describeHttpError(body: unknown): string | null {
  if (typeof body === "string") {
    return body.slice(0, 200);
  }
  if (body && typeof body === "object") {
    const record = body as Record<string, unknown>;
    const detail = record.detail;
    if (typeof detail === "string") {
      return detail;
    }
    const message = record.message;
    if (typeof message === "string") {
      return message;
    }
  }
  return null;
}

export function buildUrl(baseUrl: string, template: string, args: CallArgs): string {
  const path = template.replace(/\{(\w+)\}/g, (_all, key: string) => {
    const value = args.path?.[key];
    if (value === undefined) {
      throw new Error(`missing path parameter {${key}} for ${template}`);
    }
    return encodeURIComponent(value);
  });
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(args.query ?? {})) {
    if (value !== undefined) {
      query.set(key, value);
    }
  }
  const search = query.toString();
  return `${baseUrl}${path}${search === "" ? "" : `?${search}`}`;
}

/* ------------------------------------------------------------------ state mapping */

/**
 * Which visual state a payload belongs to. `source_kind` is authoritative for the
 * planned / committed / observed / synthetic split; uncertainty and rejection are
 * separate presentation states derived from their own endpoints, and both are additive:
 * an uncertain aircraft still shows its last observed position underneath.
 */
export function stateOf(value: string): DataState | null {
  switch (value) {
    case "planned":
    case "committed":
    case "observed":
    case "synthetic":
    case "uncertain":
    case "rejected":
      return value;
    default:
      return null;
  }
}

export function isTrustedCommit(meta: ResponseMeta): boolean {
  return meta.provenance?.authoritative === true && meta.transportMode === "live";
}
