/**
 * The reason-code table: the operator sentence beside the wire code.
 *
 * The console does not invent reasons. The ledger and the dispatcher already name every
 * refusal with a stable code and, in the decision trace, with a `human_reason` sentence
 * written server-side (`packages/reservations/.../errors.py` and
 * `packages/dispatcher/.../rejection.py`). What this table adds is only the *grouping* the
 * demo brief asks for: docs/05-chandigarh-demo.md 5.4 names seven plain-language queue
 * states, and a wire code is mapped onto exactly one of them so the queue can be scanned.
 *
 * The API's own sentence always wins as the displayed text; the bucket is a label, and the
 * code is printed next to it so nothing is paraphrased away. A code that is not in this
 * table shows as "explained by the API", never as a guessed bucket.
 *
 * Simulation only. Not authorised for flight.
 */

import type { Schemas } from "../../lib/api/types";

export type RejectionBucket =
  | "waiting-charging"
  | "pad-busy"
  | "no-feasible-route"
  | "maintenance-blocked"
  | "weather-hold"
  | "priority-accepted"
  | "duplicate-order"
  | "energy-reserve"
  | "unmapped";

export interface BucketInfo {
  readonly id: RejectionBucket;
  /** The sentence docs/05 section 5.4 asks the queue to show. */
  readonly plain: string;
  /** Why this bucket exists when the API already has a finer code. */
  readonly from: string;
}

export const BUCKETS: Record<RejectionBucket, BucketInfo> = {
  "waiting-charging": {
    id: "waiting-charging",
    plain: "waiting for charging",
    from: "docs/05-chandigarh-demo.md 5.4",
  },
  "pad-busy": {
    id: "pad-busy",
    plain: "destination pad busy",
    from: "docs/05-chandigarh-demo.md 5.4",
  },
  "no-feasible-route": {
    id: "no-feasible-route",
    plain: "no feasible route",
    from: "docs/05-chandigarh-demo.md 5.4",
  },
  "maintenance-blocked": {
    id: "maintenance-blocked",
    plain: "maintenance blocked",
    from: "docs/05-chandigarh-demo.md 5.4",
  },
  "weather-hold": {
    id: "weather-hold",
    plain: "weather hold",
    from: "docs/05-chandigarh-demo.md 5.4",
  },
  "priority-accepted": {
    id: "priority-accepted",
    plain: "priority accepted",
    from: "docs/05-chandigarh-demo.md 5.4",
  },
  "duplicate-order": {
    id: "duplicate-order",
    plain: "duplicate order",
    from: "docs/05-chandigarh-demo.md 5.4",
  },
  "energy-reserve": {
    id: "energy-reserve",
    plain: "energy reserve short",
    from: "the ledger codes are finer than the seven demo labels; energy keeps its own row",
  },
  unmapped: {
    id: "unmapped",
    plain: "explained by the API",
    from: "no bucket claimed this code, so the server's own sentence is shown unchanged",
  },
};

/**
 * Wire code -> bucket. Both namespaces are listed because both reach the console:
 * `ledger.*` from the reservation ledger's `RejectionCode`, the bare names from the
 * dispatcher's `ReasonCode`. Unlisted codes fall through to `unmapped`.
 */
const CODE_TO_BUCKET: Record<string, RejectionBucket> = {
  // ledger.RejectionCode
  "ledger.duplicate_request_resolved": "duplicate-order",
  "ledger.identity_mismatch": "duplicate-order",
  "ledger.snapshot_not_frozen": "unmapped",
  "ledger.no_eligible_aircraft": "no-feasible-route",
  "ledger.aircraft_not_operable": "maintenance-blocked",
  "ledger.no_candidate_plans": "no-feasible-route",
  "ledger.candidate_restricted": "no-feasible-route",
  "ledger.resource_unavailable": "pad-busy",
  "ledger.candidate_self_overlap": "no-feasible-route",
  "ledger.energy_margin_below_reserve": "energy-reserve",
  "ledger.recovery_not_feasible": "energy-reserve",
  "ledger.lock_order_invalid": "unmapped",
  "ledger.committed_overlap": "pad-busy",
  "ledger.unresolved_protection": "weather-hold",
  "ledger.stale_reservation_version": "unmapped",
  "ledger.committed": "priority-accepted",
  "ledger.cancelled_by_request": "unmapped",
  "ledger.cancel_refused_already_committed": "priority-accepted",
  "ledger.mission_not_cancellable": "priority-accepted",
  "ledger.cancelled_before_commit": "unmapped",
  "checker.conflict": "no-feasible-route",
  "checker.unproven": "no-feasible-route",
  "checker.allow_rule_unmatched": "no-feasible-route",
  // dispatcher.ReasonCode
  aircraft_offline: "maintenance-blocked",
  aircraft_maintenance: "maintenance-blocked",
  aircraft_busy_this_round: "pad-busy",
  aircraft_airborne: "priority-accepted",
  aircraft_link_uncertain: "unmapped",
  aircraft_observation_stale: "unmapped",
  aircraft_payload_exceeded: "no-feasible-route",
  aircraft_charger_unready: "waiting-charging",
  no_eligible_aircraft: "no-feasible-route",
  energy_margin_below_reserve: "energy-reserve",
  recovery_not_feasible: "energy-reserve",
  recovery_route_missing: "energy-reserve",
  deadline_exceeded: "no-feasible-route",
  stale_weather: "weather-hold",
  profile_unknown: "unmapped",
  pad_capacity_queue: "pad-busy",
  charger_queue: "waiting-charging",
  crew_unavailable: "maintenance-blocked",
  facility_outage: "pad-busy",
  outside_opening_hours: "pad-busy",
  horizon_exceeded: "no-feasible-route",
  airborne_commitment_immutable: "priority-accepted",
  sla_has_no_emergency_right: "priority-accepted",
  time_window_infeasible: "no-feasible-route",
  solver_infeasible: "no-feasible-route",
  starvation_bound_reached: "priority-accepted",
  dispatch_input_invalid: "unmapped",
  // contract / transport level
  duplicate_commit_conflict: "duplicate-order",
  idempotency_key_reused: "duplicate-order",
  committed: "priority-accepted",
  ok: "priority-accepted",
};

export interface ExplainedReason {
  readonly code: string;
  readonly bucket: BucketInfo;
  /** The API's own sentence, when the trace carried one. */
  readonly humanReason: string | null;
  /** Everything known about this code in one line, for the `title` attribute. */
  readonly hint: string;
}

const ENERGY_ALIASES = new Set([
  "energy_reserve_shortfall",
  "energy_margin_below_reserve",
  "ledger.energy_margin_below_reserve",
]);

export function explainReason(code: string | undefined, apiSentence?: string): ExplainedReason {
  const wire = code ?? "";
  // The reference deployment's fixture trace uses `energy_reserve_shortfall`; the ledger's
  // wire value is `ledger.energy_margin_below_reserve`. Both mean "cannot keep its reserve".
  const lookup = ENERGY_ALIASES.has(wire) ? "ledger.energy_margin_below_reserve" : wire;
  const bucket = BUCKETS[CODE_TO_BUCKET[lookup] ?? "unmapped"];
  return {
    code: wire === "" ? "no code on record" : wire,
    bucket,
    humanReason: apiSentence ?? null,
    hint:
      `wire code: ${wire === "" ? "none" : wire}\n` +
      `queue label "${bucket.plain}" comes from ${bucket.from}\n` +
      `the sentence below is the API's, not the console's: ${apiSentence ?? "the trace carried none"}`,
  };
}

/** Why a mission is what it is, from the trace the server stored for it. */
export function reasonOfMission(trace: Schemas["DecisionTraceResponse"] | null): ExplainedReason {
  if (trace === null) {
    return explainReason(undefined, undefined);
  }
  const firstRejected = trace.rejected?.[0];
  const code =
    trace.primary_reason_code ?? firstRejected?.reason_code ?? trace.record.reason_codes[0];
  return explainReason(code, trace.primary_human_reason ?? firstRejected?.human_reason);
}

/** Every rejection on one mission, in the order the transaction produced them. */
export function rejectionsOf(
  trace: Schemas["DecisionTraceResponse"] | null,
): readonly Schemas["RejectedCandidate"][] {
  return trace?.rejected ?? [];
}

export function isBlockedReason(reason: ExplainedReason): boolean {
  return (
    reason.bucket.id !== "priority-accepted" &&
    reason.bucket.id !== "duplicate-order" &&
    reason.code !== "no code on record"
  );
}
