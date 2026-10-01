/**
 * Gate for the Phase 4 exit criterion "UI is fully typed from contracts".
 *
 * Fails if the committed generator output is missing, older than the contract it came
 * from, contains an `any`, or lost a schema the console depends on.
 *
 * Simulation only. Not authorised for flight.
 */

import { readFileSync, statSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, "..");
const generatedPath = join(root, "src", "generated", "api-types.ts");
const contractPath = join(root, "..", "..", "contracts", "openapi", "v1.yaml");

const EXPECTED_SCHEMAS = [
  "OrderRequest",
  "OrderResponse",
  "MissionResponse",
  "PlanVersionResponse",
  "ReservationResponse",
  "DecisionTraceResponse",
  "RejectedCandidate",
  "EligibleAircraft",
  "CandidateRanking",
  "DecisionInputs",
  "TelemetryObservationResponse",
  "UncertaintyStateResponse",
  "AircraftResponse",
  "FacilityResponse",
  "FacilityCapacityResponse",
  "AuditEventResponse",
  "SimulationRunResponse",
  "ReplayDiffResponse",
  "ScenarioComparisonResponse",
  "StatusStreamEvent",
  "ErrorResponse",
  "ConflictResponse",
];

const problems = [];
let source;
try {
  source = readFileSync(generatedPath, "utf8");
} catch {
  console.error(`missing ${generatedPath} - run \`npm run generate:types\``);
  process.exit(1);
}

const generatedTime = statSync(generatedPath).mtimeMs;
const contractTime = statSync(contractPath).mtimeMs;
if (generatedTime < contractTime) {
  problems.push(
    `generated output is older than contracts/openapi/v1.yaml (${new Date(generatedTime).toISOString()} < ${new Date(contractTime).toISOString()}) - re-run \`npm run generate:types\``,
  );
}

const anyMatches = [...source.matchAll(/:\s*any\b|\bany\s*\||<any>/g)];
if (anyMatches.length > 0) {
  problems.push(`${anyMatches.length} occurrence(s) of \`any\` in the generated types`);
}

for (const schema of EXPECTED_SCHEMAS) {
  if (!source.includes(`${schema}:`)) {
    problems.push(`schema ${schema} is not in the generated output`);
  }
}

const operationIds = [...source.matchAll(/^ {4}(\w+): \{$/gm)].length;
const noticeCount = (source.match(/Simulation only\. Not authorised for flight\./g) ?? []).length;

console.log(`generated:  src/generated/api-types.ts (${source.split("\n").length} lines)`);
console.log(`contract:   ${contractPath.replace(/\\/g, "/")}`);
console.log(
  `staleness:  generated ${generatedTime >= contractTime ? "is not older than" : "IS OLDER THAN"} the contract`,
);
console.log(`any:        ${anyMatches.length} occurrences`);
console.log(
  `schemas:    ${EXPECTED_SCHEMAS.length}/${EXPECTED_SCHEMAS.length} required names present`,
);
console.log(
  `notices:    ${noticeCount} responses document the standing statement in their description`,
);
console.log(`operations: ${operationIds} top-level operation blocks`);

if (problems.length > 0) {
  console.error("\nFAIL");
  for (const problem of problems) {
    console.error(` - ${problem}`);
  }
  process.exit(1);
}
console.log("\nPASS: the console's payload types come from the contract, not from hand-writing.");
