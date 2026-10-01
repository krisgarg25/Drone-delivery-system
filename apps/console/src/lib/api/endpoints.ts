/**
 * Named operations over the typed client, plus the two operational-support probes and the
 * status stream. Every function returns `ApiResult<T>` where `T` is projected from the
 * generated contract types - no payload shape is written by hand in this file.
 *
 * The default credential is the simulation deployment's READ-ONLY observer
 * (`sim-observer-a`, apps/api/src/droneops_api/composition.py). Override with
 * `VITE_DRONEOPS_TOKEN`; there is no writable console credential, because the console
 * has no writable operations.
 *
 * Simulation only. Not authorised for flight.
 */

import { bearerToken, createHttpTransport, SIMULATION_NOTICE } from "./client";
import type { ApiResult, CallArgs, Transport, ReadOperationId } from "./client";
import type { HealthResponse, ResponseFor, Schemas, VersionResponse } from "./types";

export interface Api {
  readonly transport: Transport;
  call<Id extends ReadOperationId>(id: Id, args?: CallArgs): Promise<ApiResult<ResponseFor<Id>>>;
  facilities(): Promise<ApiResult<ResponseFor<"listFacilities">>>;
  facilityCapacity(id: string): Promise<ApiResult<ResponseFor<"getFacilityCapacity">>>;
  aircraft(): Promise<ApiResult<ResponseFor<"listAircraft">>>;
  aircraftById(id: string): Promise<ApiResult<ResponseFor<"getAircraft">>>;
  orders(query?: {
    time_from?: string;
    time_to?: string;
  }): Promise<ApiResult<ResponseFor<"listOrders">>>;
  orderById(id: string): Promise<ApiResult<ResponseFor<"getOrder">>>;
  reservations(from: string, to: string): Promise<ApiResult<ResponseFor<"listReservations">>>;
  missionReservations(id: string): Promise<ApiResult<ResponseFor<"listMissionReservations">>>;
  planVersions(id: string): Promise<ApiResult<ResponseFor<"listMissionPlanVersions">>>;
  decisionTrace(id: string): Promise<ApiResult<ResponseFor<"getMissionDecisionTrace">>>;
  uncertainty(id: string): Promise<ApiResult<ResponseFor<"getMissionUncertaintyState">>>;
  observations(id: string): Promise<ApiResult<ResponseFor<"listMissionObservations">>>;
  auditEvents(from: string, to: string): Promise<ApiResult<ResponseFor<"listAuditEvents">>>;
  simulationRun(runId: string): Promise<ApiResult<ResponseFor<"getSimulationRun">>>;
  replayDiff(runId: string): Promise<ApiResult<ResponseFor<"getSimulationReplayDiff">>>;
  compareScenarios(
    scenarioId: string,
    against: string,
  ): Promise<ApiResult<ResponseFor<"compareScenarios">>>;
  health(): Promise<HealthResponse | null>;
  version(): Promise<VersionResponse | null>;
}

export function createApi(transport: Transport): Api {
  const call = <Id extends ReadOperationId>(id: Id, args: CallArgs = {}) => transport.get(id, args);
  const probe = async <T>(path: string): Promise<T | null> => {
    try {
      const response = await fetch(path, {
        headers: { accept: "application/json", authorization: `Bearer ${bearerToken()}` },
      });
      if (!response.ok) {
        return null;
      }
      return (await response.json()) as T;
    } catch {
      return null;
    }
  };

  return {
    transport,
    call,
    facilities: () => call("listFacilities"),
    facilityCapacity: (id) => call("getFacilityCapacity", { path: { id } }),
    aircraft: () => call("listAircraft"),
    aircraftById: (id) => call("getAircraft", { path: { id } }),
    orders: (query) => call("listOrders", { query: { ...query } }),
    orderById: (id) => call("getOrder", { path: { id } }),
    reservations: (time_from, time_to) =>
      call("listReservations", { query: { time_from, time_to } }),
    missionReservations: (id) => call("listMissionReservations", { path: { id } }),
    planVersions: (id) => call("listMissionPlanVersions", { path: { id } }),
    decisionTrace: (id) => call("getMissionDecisionTrace", { path: { id } }),
    uncertainty: (id) => call("getMissionUncertaintyState", { path: { id } }),
    observations: (id) => call("listMissionObservations", { path: { id } }),
    auditEvents: (time_from, time_to) => call("listAuditEvents", { query: { time_from, time_to } }),
    simulationRun: (run_id) => call("getSimulationRun", { path: { run_id } }),
    replayDiff: (run_id) => call("getSimulationReplayDiff", { path: { run_id } }),
    compareScenarios: (scenario_id, against) =>
      call("compareScenarios", { path: { scenario_id }, query: { against } }),
    health: () => probe<HealthResponse>("/health"),
    version: () => probe<VersionResponse>("/version"),
  };
}

/* ------------------------------------------------------------------ status stream */

export interface StatusStream {
  close(): void;
}

/**
 * `readStatusStream` is `text/event-stream`, so it is not a JSON call. Each event declares
 * its own `source_kind` (see the contract description), which is what lets the shell keep
 * a planned event out of the committed layer. The stream closes on its own; this wrapper
 * never reconnects into an unbounded live feed.
 */
export function openStatusStream(
  onEvent: (event: Schemas["StatusStreamEvent"]) => void,
  options: { after?: number; limit?: number } = {},
): StatusStream {
  const params = new URLSearchParams({
    after: String(options.after ?? 0),
    limit: String(options.limit ?? 100),
  });
  const source = new EventSource(`/v1/status/stream?${params.toString()}`);
  source.addEventListener("message", (event: MessageEvent<string>) => {
    try {
      onEvent(JSON.parse(event.data) as Schemas["StatusStreamEvent"]);
    } catch {
      /* A malformed frame is dropped and reported, never guessed at. */
    }
  });
  return {
    close: () => {
      source.close();
    },
  };
}

export { bearerToken, createHttpTransport, SIMULATION_NOTICE };
