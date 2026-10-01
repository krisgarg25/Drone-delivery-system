/**
 * Replay and timeline.
 *
 * Two rules govern this panel, and they are the reason it looks the way it does:
 *
 *  1. A replay shows the exact input snapshot the run used, never the current database
 *     state (technical-build/05, Console principles; docs/05 section 5.4). The frame list
 *     comes from the stored observations, and the panel prints the snapshot digest, the
 *     frozen-at stamp, and the live world digest it did NOT use, side by side, so the
 *     claim is checkable rather than asserted.
 *  2. Uncertainty is never smoothed away. Frames are discrete stored observations: there is
 *     no interpolation between them, the track declares no transition, and under
 *     `prefers-reduced-motion` playback is not offered at all - only stepping. A position
 *     that eased from A to B would read as measured movement that was never measured.
 *
 * Planned route and observed telemetry are two layers, in the panel's own strip and on the
 * map, with the palette's second channels (dash and glyph) doing the separating.
 *
 * Simulation only. Not authorised for flight.
 */

import type { ApiError } from "../lib/api/client";
import type { Schemas } from "../lib/api/types";
import { STATE_STYLES } from "../lib/palette";
import { WORLD, corridorFor, pointAlong } from "../map/world";
import type { Panel, PanelContext } from "../shell/panels";
import {
  block,
  button,
  el,
  emptyState,
  errorState,
  field,
  fieldList,
  hashLine,
  loadingState,
  mono,
  paragraph,
  provenanceLine,
  sectionTitle,
  shortId,
  takeHandoff,
} from "./support/kit";
import "./panels.css";

type Observation = Schemas["TelemetryObservationResponse"];
type Plan = Schemas["PlanVersionResponse"];
type Reservation = Schemas["ReservationResponse"];

const SVG_NS = "http://www.w3.org/2000/svg";

function svg<K extends keyof SVGElementTagNameMap>(
  tag: K,
  attrs: Record<string, string>,
): SVGElementTagNameMap[K] {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [name, value] of Object.entries(attrs)) {
    node.setAttribute(name, value);
  }
  return node;
}

interface ReplayData {
  readonly missionId: string;
  readonly observations: readonly Observation[];
  readonly plans: readonly Plan[];
  readonly reservations: readonly Reservation[];
  readonly uncertainty: Schemas["UncertaintyStateResponse"] | null;
  readonly run: Schemas["SimulationRunResponse"] | null;
  readonly diff: Schemas["ReplayDiffResponse"] | null;
  readonly failures: readonly string[];
}

interface Frame {
  readonly index: number;
  readonly atUs: number;
  readonly label: string;
  readonly observation: Observation | null;
  readonly stale: boolean;
}

async function gather(
  api: PanelContext["api"],
  missionId: string,
  scenarioId: string,
): Promise<{ ok: true; data: ReplayData } | { ok: false; error: ApiError }> {
  const [observations, plans, reservations, uncertainty, run, diff] = await Promise.all([
    api.observations(missionId),
    api.planVersions(missionId),
    api.missionReservations(missionId),
    api.uncertainty(missionId),
    api.simulationRun(scenarioId),
    api.replayDiff(scenarioId),
  ]);
  if (!observations.ok && !plans.ok) {
    return { ok: false, error: observations.error };
  }
  const failures: string[] = [];
  for (const [subject, result] of [
    ["observations", observations],
    ["plan versions", plans],
    ["reservations", reservations],
    ["uncertainty", uncertainty],
    ["run snapshot", run],
    ["replay diff", diff],
  ] as const) {
    if (!result.ok) {
      failures.push(`${subject}: ${result.error.message}`);
    }
  }
  return {
    ok: true,
    data: {
      missionId,
      observations: observations.ok ? observations.data.items : [],
      plans: plans.ok ? plans.data.items : [],
      reservations: reservations.ok ? reservations.data.items : [],
      uncertainty: uncertainty.ok ? uncertainty.data : null,
      run: run.ok ? run.data : null,
      diff: diff.ok ? diff.data : null,
      failures,
    },
  };
}

/**
 * Frames are the stored observations themselves. Nothing is generated between them: a
 * replay with three observations has three frames, and the gap between them is drawn as a
 * gap.
 */
function framesFor(data: ReplayData): Frame[] {
  const ordered = [...data.observations].sort(
    (a, b) => Date.parse(a.measurement_time) - Date.parse(b.measurement_time),
  );
  const start = ordered.length === 0 ? 0 : Date.parse(ordered[0].measurement_time);
  return ordered.map((observation, index) => ({
    index,
    atUs: (Date.parse(observation.measurement_time) - start) * 1000,
    label: `t+${((Date.parse(observation.measurement_time) - start) / 1000).toFixed(0)}s`,
    observation,
    stale:
      data.uncertainty !== null &&
      data.uncertainty.state !== "clear" &&
      Date.parse(observation.measurement_time) >=
        Date.parse(data.uncertainty.last_accepted_measurement_time ?? "9999-01-01T00:00:00Z"),
  }));
}

/* ------------------------------------------------------------------ the track */

function track(
  frames: readonly Frame[],
  position: number,
  plans: readonly Plan[],
  context: PanelContext,
): HTMLElement {
  const width = 900;
  const height = 120;
  const canvas = svg("svg", {
    class: "rp-track",
    viewBox: `0 0 ${width} ${height}`,
    role: "img",
    "aria-label": `Replay track: ${String(frames.length)} stored observation frames on the frozen corridor geometry, frame ${String(position + 1)} shown.`,
  });

  // Planned route: the corridor the committed plan version reserved, in the planned
  // channel (dashed, ◌, PLAN).
  plans.forEach((plan, planIndex) => {
    const keys = plan.resource_keys ?? [];
    for (const key of keys) {
      const corridor = corridorFor(key.replace(/^corridor:/, ""));
      if (corridor === undefined) {
        continue;
      }
      const points: string[] = [];
      for (let step = 0; step <= 24; step += 1) {
        const [lon, lat] = pointAlong(corridor, step / 24);
        points.push(
          `${(((lon - 76.5) / 0.6) * width).toFixed(1)},${(24 + planIndex * 14 + (lat - 30.6) * 4).toFixed(1)}`,
        );
      }
      canvas.append(
        svg("polyline", {
          points: points.join(" "),
          fill: "none",
          stroke: "var(--state-planned)",
          "stroke-width": "2",
          "stroke-dasharray": STATE_STYLES.planned.dashCss,
        }),
      );
    }
  });

  // Observed telemetry: only frames up to the scrub position, dotted with the measured
  // glyph, never joined across a gap.
  const shown = frames.slice(0, position + 1);
  shown.forEach((frame) => {
    const x = ((frame.index + 0.5) / Math.max(1, frames.length)) * width;
    canvas.append(
      svg("circle", {
        cx: x.toFixed(1),
        cy: "86",
        r: frame.observation?.out_of_order === true ? "6" : "4",
        fill: frame.stale ? "var(--state-uncertain)" : "var(--state-observed)",
        stroke: "var(--map-basemap)",
        "stroke-width": "1.5",
      }),
    );
  });

  // The uncertainty band: the span of frames the ledger considered stale. Drawn as a
  // hatched rectangle with its own boundary, because a smooth fade would be the exact
  // thing the brief forbids.
  const stale = frames.filter((frame) => frame.stale);
  if (stale.length > 0) {
    const first = stale[0];
    const last = stale[stale.length - 1];
    const x = ((first.index + 0.5) / Math.max(1, frames.length)) * width;
    const right = ((last.index + 1.5) / Math.max(1, frames.length)) * width;
    canvas.append(
      svg("rect", {
        x: x.toFixed(1),
        y: "70",
        width: (right - x).toFixed(1),
        height: "34",
        fill: "var(--state-uncertain)",
        "fill-opacity": String(STATE_STYLES.uncertain.fillOpacity),
        stroke: "var(--state-uncertain)",
        "stroke-dasharray": STATE_STYLES.uncertain.dashCss,
        "stroke-width": "1",
      }),
    );
  }

  // The playhead. No CSS transition is declared on this element anywhere in the console:
  // moving to a frame is a discrete act.
  const head = ((position + 0.5) / Math.max(1, frames.length)) * width;
  canvas.append(
    svg("line", {
      x1: head.toFixed(1),
      x2: head.toFixed(1),
      y1: "8",
      y2: "112",
      stroke: "var(--focus)",
      "stroke-width": "2",
    }),
  );
  const wrap = el("div", { class: "rp-track-wrap" });
  wrap.append(canvas);
  if (!context.layers.visible("observed-telemetry")) {
    wrap.append(
      el("p", {
        class: "kit-state-hint",
        text: "The observed layer is switched off on the map; the frames are still listed here because the strip and the canvas are separate views of the same stored rows.",
      }),
    );
  }
  return wrap;
}

function frameSummary(frame: Frame | undefined, frames: readonly Frame[]): HTMLElement {
  if (frame === undefined) {
    return emptyState(
      "the selected frame",
      "There is no frame at this position.",
      "a stored observation on the timeline.",
    );
  }
  if (frame.observation === null) {
    return emptyState(
      "the selected frame",
      "There is no stored observation at this position.",
      "a stored observation on the timeline.",
    );
  }
  const position = frame.observation.position;
  return block(
    "dl",
    { class: "kit-fields rp-frame-list", aria: { label: "the frame under the playhead" } },
    [
      field(
        "frame",
        `${frame.index + 1} of ${frames.length} stored observations`,
        "frames are the rows the API returned; none were generated between them",
      ),
      field("measurement time", `${frame.label} (${frame.observation.measurement_time})`),
      field("received time", frame.observation.received_time),
      field("sequence", String(frame.observation.sequence)),
      field(
        "position",
        `${position.lat.toFixed(5)}, ${position.lon.toFixed(5)} at ${position.altitude_m} ${position.altitude_reference}`,
        "altitude_reference is part of the contract: an altitude without a reference is not renderable as a height",
      ),
      field(
        "out of order",
        frame.observation.out_of_order === true
          ? "yes - this frame arrived late or reordered"
          : "no",
        "a late frame cannot overwrite a newer measurement (invariant I10); it is shown where it was measured",
      ),
      field("source", frame.observation.source ?? "not stated"),
      ...(frame.stale
        ? [
            field(
              "uncertainty",
              "this frame is inside the stale window the ledger reports",
              "the band on the track is drawn from UncertaintyStateResponse, not inferred here",
            ),
          ]
        : []),
    ],
  );
}

/* ------------------------------------------------------------------ panel body */

function render(
  host: HTMLElement,
  context: PanelContext,
  data: ReplayData,
  frames: readonly Frame[],
): void {
  let position = 0;
  let playing = false;
  let timer: number | null = null;
  const reduced = context.reducedMotion();

  host.replaceChildren();
  host.append(
    paragraph(
      "A replay reads the snapshot the run was given. The digests below are printed with the live world digest beside them so the difference is visible rather than trusted.",
    ),
  );

  // The frozen input snapshot, named.
  const snapshotRows = [
    hashLine("snapshot digest used", data.run?.snapshot_digest ?? "not stated by the run"),
    hashLine("bundle manifest", WORLD.provenance.bundle_manifest_sha256),
    field("frozen at", data.run?.frozen_at ?? "the run reported no frozen_at"),
    field(
      "replayed against",
      data.diff?.replayed_against ?? "not stated by getSimulationReplayDiff",
    ),
    hashLine("event hash", data.run?.event_hash ?? "not stated"),
    hashLine("current world digest (NOT used)", data.diff?.current_world_digest ?? "not stated"),
    field(
      "digests still equal?",
      data.diff === null
        ? "not answered"
        : `event hash equal: ${String(data.diff.event_hash_equal)}; metrics equal: ${String(data.diff.metrics_equal)}${
            (data.diff.differing_metrics ?? []).length > 0
              ? `; differing: ${(data.diff.differing_metrics ?? []).join(", ")}`
              : ""
          }`,
      "this is the run's own claim about reproducing itself, read from /v1/simulations/{run_id}/replay-diff",
    ),
    field(
      "seed / policy / engine",
      `${String(data.run?.run_identity.seed ?? WORLD.provenance.seed)} · ${data.run?.run_identity.policy_version ?? WORLD.provenance.policy_version} · ${data.run?.run_identity.engine_version ?? "not stated"}`,
    ),
  ];
  host.append(
    sectionTitle("The frozen input snapshot"),
    fieldList(snapshotRows, "frozen snapshot identity"),
  );

  // Layer separation.
  const layerRows = el("div", { class: "rp-layers" });
  for (const [id, label, meaning] of [
    [
      "proposed-plan",
      "Planned route",
      `${STATE_STYLES.planned.glyph} dashed, tag ${STATE_STYLES.planned.tag}: what the planner intended`,
    ],
    [
      "observed-telemetry",
      "Observed telemetry",
      `${STATE_STYLES.observed.glyph} dotted, tag ${STATE_STYLES.observed.tag}: what the aircraft reported`,
    ],
    [
      "uncertainty-volume",
      "Uncertainty volume",
      `${STATE_STYLES.uncertain.glyph} hatched, tag ${STATE_STYLES.uncertain.tag}: where the last known position stops being true`,
    ],
  ] as const) {
    const checkbox = el("input", {
      class: "rp-layer-toggle",
      id: `rp-layer-${id}`,
      type: "checkbox",
    });
    checkbox.checked = context.layers.visible(id);
    checkbox.addEventListener("change", () => {
      context.layers.setVisible(id, checkbox.checked);
      context.announce(`${label} layer ${checkbox.checked ? "shown" : "hidden"}.`);
    });
    layerRows.append(
      block("div", { class: "rp-layer" }, [
        checkbox,
        el("label", { text: label, for: `rp-layer-${id}` }),
        el("span", { class: "mq-sub", text: meaning }),
      ]),
    );
  }
  host.append(sectionTitle("Planned and observed are separate layers"), layerRows);

  if (frames.length === 0) {
    host.append(
      emptyState(
        "the replay timeline",
        `The run stored no telemetry observations for mission ${shortId(data.missionId)}, so there is nothing to step through. The planned route above is still what the planner committed.`,
        "at least one TelemetryObservationResponse row for this mission.",
      ),
    );
    return;
  }

  const scrub = el("input", {
    class: "rp-scrub",
    type: "range",
    id: "rp-scrubber",
    aria: {
      label: "Replay position: step through the stored observation frames",
      description:
        "Left and right arrow keys move one stored frame. Home and End jump to the first and last. There is no interpolation between frames.",
    },
  });
  scrub.min = "0";
  scrub.max = String(frames.length - 1);
  scrub.step = "1";
  scrub.value = String(position);

  const summary = el("div", { class: "rp-summary" });
  const clock = el("p", { class: "rp-clock" });
  const trackHost = el("div", { class: "rp-track-host" });
  const controls = el("div", { class: "rp-controls" });
  const playState = el("span", { class: "rp-playing", role: "status" });

  const paint = (): void => {
    scrub.value = String(position);
    const frame = frames.at(position);
    clock.textContent = `${frame?.label ?? "no frame"} · frame ${String(position + 1)} of ${String(frames.length)} · ${frame?.stale === true ? "inside the stale window" : "measurement accepted in sequence"}`;
    trackHost.replaceChildren(track(frames, position, data.plans, context));
    summary.replaceChildren(frameSummary(frame, frames));
  };

  const step = (delta: number, origin: string): void => {
    const next = Math.min(frames.length - 1, Math.max(0, position + delta));
    if (next === position) {
      context.announce(`${origin}: already at the ${delta < 0 ? "first" : "last"} stored frame.`);
      return;
    }
    position = next;
    paint();
    context.announce(
      `Frame ${String(position + 1)} of ${String(frames.length)}. ${frames[position]?.label ?? ""}`,
    );
  };

  const stop = (): void => {
    playing = false;
    if (timer !== null) {
      window.clearInterval(timer);
      timer = null;
    }
    playState.textContent = "paused";
  };

  controls.append(
    button(
      "Step back one frame",
      () => {
        stop();
        step(-1, "Step back");
      },
      { ariaLabel: "Step back one stored frame. Shortcut: left arrow." },
    ),
    button(
      "Step forward one frame",
      () => {
        stop();
        step(1, "Step forward");
      },
      { ariaLabel: "Step forward one stored frame. Shortcut: right arrow." },
    ),
    button("Jump to the first frame", () => {
      stop();
      position = 0;
      paint();
    }),
    button("Jump to the last frame", () => {
      stop();
      position = frames.length - 1;
      paint();
    }),
  );

  if (reduced) {
    controls.append(
      el("p", {
        class: "rp-no-motion",
        text: "Playback is not offered: the system reports prefers-reduced-motion, and this console does not animate a data state under any setting. Step with the buttons or the arrow keys.",
      }),
    );
  } else {
    const toggle = el("button", {
      class: "kit-button",
      type: "button",
      aria: { label: "Play or pause the replay" },
    });
    const label = (): void => {
      toggle.textContent = playing ? "Pause the replay" : "Play, one frame every 900 ms";
    };
    label();
    label();
    toggle.addEventListener("click", () => {
      if (playing) {
        stop();
        label();
        return;
      }
      playing = true;
      playState.textContent = "playing";
      label();
      timer = window.setInterval(() => {
        if (position >= frames.length - 1) {
          stop();
          label();
          return;
        }
        position += 1;
        paint();
      }, 900);
    });
    controls.append(toggle, playState);
  }

  scrub.addEventListener("input", () => {
    stop();
    position = Math.min(frames.length - 1, Math.max(0, Number(scrub.value)));
    paint();
  });

  const region = el("div", {
    class: "rp-timeline",
    role: "group",
    aria: { label: "Replay timeline controls" },
  });
  region.addEventListener("keydown", (event: KeyboardEvent) => {
    if (event.key === "ArrowLeft") {
      event.preventDefault();
      step(-1, "Arrow key");
    } else if (event.key === "ArrowRight") {
      event.preventDefault();
      step(1, "Arrow key");
    } else if (event.key === "Home") {
      event.preventDefault();
      position = 0;
      paint();
    } else if (event.key === "End") {
      event.preventDefault();
      position = frames.length - 1;
      paint();
    }
  });
  region.append(
    el("label", { text: "Scrub the stored frames", for: "rp-scrubber" }),
    block("div", { class: "rp-controls-row" }, [scrub, clock]),
    controls,
    trackHost,
    summary,
  );
  host.append(sectionTitle("Timeline"), region);

  if (frames.length < 2) {
    host.append(
      block("div", { class: "kit-state-wrap kit-state-wrap--boundary" }, [
        el("h4", { class: "kit-state-heading", text: "One frame is not a trajectory" }),
        el("p", {
          class: "kit-state",
          text: "This mission has a single stored observation, so the replay shows one position and an empty gap where motion would be. Nothing is drawn between them.",
        }),
        el("p", { class: "kit-state-hint", text: "So the figure below is: a point, not a path." }),
      ]),
    );
  }

  host.append(
    sectionTitle("What the ledger held while the replay ran"),
    data.reservations.length === 0
      ? mono("no reservation rows for this mission")
      : fieldList(
          data.reservations.map((row) =>
            field(
              `${row.resource_kind} · ${row.state}`,
              `${row.resource_key} ${row.valid_from} → ${row.valid_until}`,
              "the window the ledger held; unresolved rows stay held until telemetry reconciles",
            ),
          ),
          "reservations during the replay window",
        ),
  );
  if (data.uncertainty !== null) {
    host.append(
      context.stateChip("uncertain", `telemetry ${data.uncertainty.state}`),
      fieldList(
        [
          field("state", data.uncertainty.state),
          field(
            "staleness",
            data.uncertainty.staleness_s === undefined
              ? "not stated"
              : `${data.uncertainty.staleness_s} s`,
          ),
          field("protected volume retained", String(data.uncertainty.protected_volume_retained)),
        ],
        "uncertainty on the frozen snapshot",
      ),
    );
  }
  if (data.failures.length > 0) {
    host.append(
      el("p", {
        class: "dt-partials",
        role: "status",
        text: `Sources that did not answer: ${data.failures.join(" | ")}`,
      }),
    );
  }
  paint();
}

/* ------------------------------------------------------------------ panel */

export const replayPanel: Panel = {
  id: "replay",
  slot: "replay.controls",
  title: "Replay and timeline",
  description:
    "The frozen snapshot the run read, a discrete frame scrubber with keyboard stepping, and planned route and observed telemetry as two separate layers.",
  mount(host, context) {
    host.dataset.panelRoot = "replay";
    let disposed = false;
    const selection = context.selection.get();
    const missionId = selection.missionId;
    if (missionId === null) {
      host.replaceChildren(
        emptyState(
          "the replay",
          "No mission is in scope, so there is no observation set to step through. Pick a refused mission in the queue, or one on the map.",
          "a mission selection; the trace panel offers a one-key jump to this screen.",
        ),
        button("Go to the mission queue", () => {
          location.hash = "#/missions";
        }),
      );
      return;
    }
    host.append(
      loadingState(
        `the frozen observations for mission ${shortId(missionId)}`,
        "observations, plan versions, reservations, the run snapshot and the replay diff.",
      ),
    );
    void gather(context.api, missionId, selection.scenarioId).then((result) => {
      if (disposed || !document.body.contains(host)) {
        return;
      }
      if (!result.ok) {
        host.replaceChildren(
          errorState(result.error, () => {
            location.hash = "#/replay";
          }),
        );
        return;
      }
      const data = result.data;
      render(host, context, data, framesFor(data));
      host.append(
        provenanceLine(context, {
          transportMode: context.api.transport.mode,
          transportLabel: context.api.transport.label,
          operation: "listMissionObservations",
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
      if (takeHandoff("replay", missionId)) {
        const scrubber = document.getElementById("rp-scrubber");
        if (scrubber instanceof HTMLInputElement) {
          scrubber.focus();
          context.announce(
            "Replay open and focused on the scrubber. Left and right arrow keys step one stored frame at a time.",
          );
        }
      }
    });
    context.onDispose(() => {
      disposed = true;
    });
  },
};
