/**
 * The operations panels, in the order the shell mounts them.
 *
 * `src/shell/panels.ts` documents the seam: a panel declares the slot it fills, the shell
 * mounts it there on the route that owns the slot, and no other panel or shell module has
 * to know it exists. Nothing in this file decides, ranks or validates a mission - each
 * module reads the contract and renders it.
 *
 * Simulation only. Not authorised for flight.
 */

import type { Panel } from "../shell/panels";
import { comparisonPanel } from "./comparison";
import { decisionTracePanel } from "./decision-trace";
import { economicsPanel } from "./economics";
import { exceptionPanel } from "./exceptions";
import { fleetPanel } from "./fleet";
import { missionBoardPanel } from "./mission-board";
import { missionQueuePanel } from "./mission-queue";
import { replayPanel } from "./replay";

export const OPERATIONS_PANELS: readonly Panel[] = [
  missionQueuePanel,
  missionBoardPanel,
  fleetPanel,
  economicsPanel,
  decisionTracePanel,
  replayPanel,
  comparisonPanel,
  exceptionPanel,
];
