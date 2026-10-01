/**
 * Contract-derived types. Nothing in this file re-declares a payload shape: every type is
 * projected out of `src/generated/api-types.ts`, which `npm run generate:types` produces
 * from `contracts/openapi/v1.yaml`. If the contract changes, these types change with it.
 *
 * Simulation only. Not authorised for flight.
 */

import type { components, operations } from "../../generated/api-types";

export type Schemas = components["schemas"];
export type Schema<K extends keyof Schemas> = Schemas[K];

/** The four provenance kinds the contract allows. This IS the console's data vocabulary. */
export type SourceKind = Schemas["ReservationResponse"]["source_kind"];

/**
 * The console's presentation states: the contract's four source kinds plus the two states
 * the ledger reports separately from provenance (a refused candidate, and telemetry that
 * went stale). Both are derived from contract fields, not invented:
 * `rejected` from `RejectedCandidate`, `uncertain` from `UncertaintyStateResponse.state`.
 */
export type DataState = SourceKind | "uncertain" | "rejected";

export type UncertaintyState = Schemas["UncertaintyStateResponse"]["state"];
export type ReservationState = Schemas["ReservationResponse"]["state"];

/** Every field a response must carry to be displayable as data rather than as a claim. */
export interface Provenance {
  phase: string;
  source_kind: SourceKind;
  authoritative: boolean;
  simulation_notice: string;
}

export type OperationId = keyof operations;

export type ResponseFor<Id extends OperationId> = operations[Id] extends {
  responses: { 200: { content: { "application/json": infer T } } };
}
  ? T
  : never;

export type ParamsFor<Id extends OperationId> = operations[Id] extends { parameters: infer P }
  ? P
  : never;

/** Path + query params only; `never` slots emitted by the generator are dropped. */
export type PathParams<Id extends OperationId> = Exclude<
  ParamsFor<Id> extends { path?: infer T } ? T : never,
  undefined
>;

export type QueryParams<Id extends OperationId> = Exclude<
  ParamsFor<Id> extends { query?: infer T } ? T : never,
  undefined
>;

export type HeaderParams<Id extends OperationId> = Exclude<
  ParamsFor<Id> extends { header?: infer T } ? T : never,
  undefined
>;

export type ErrorFor<Id extends OperationId> = operations[Id] extends {
  responses: { 400: { content: { "application/json": infer T } } };
}
  ? T
  : never;

/* ------------------------------------------------------------------ contract gaps
 * /health and /version are served by apps/api but deliberately excluded from the
 * reviewed business contract (see apps/api/src/droneops_api/schemas.py: "Operational
 * liveness. Not part of the reviewed business contract."). They are the only two
 * payloads in the console that are not generated, so they are isolated here and marked.
 */
export interface HealthResponse {
  status: string;
  phase: string;
  control_plane_phase: string;
  authorized_for_flight: boolean;
  detail: string;
}

export interface VersionResponse {
  version: string;
  api_version: string;
  domain_schema_version: number;
  config_release_sha: string;
  simulation_notice: string;
}

/** Anything the console renders must answer these three questions out loud. */
export function provenanceOf(value: object): Provenance | null {
  const candidate = value as Partial<Provenance>;
  if (
    typeof candidate.phase === "string" &&
    typeof candidate.source_kind === "string" &&
    typeof candidate.authoritative === "boolean" &&
    typeof candidate.simulation_notice === "string"
  ) {
    return {
      phase: candidate.phase,
      source_kind: candidate.source_kind,
      authoritative: candidate.authoritative,
      simulation_notice: candidate.simulation_notice,
    };
  }
  return null;
}
