/**
 * The data-provenance strip.
 *
 * The demo rules require the scenario panel to show the scenario id, the source, the
 * retrieval date and the hash (docs/05-chandigarh-demo.md 5.10: "attribution and scenario
 * provenance are visible"). This module assembles exactly those four plus the two the
 * console needs to be honest about itself: which transport answered, and how old the
 * weather snapshot is.
 *
 * Simulation only. Not authorised for flight.
 */

import { WORLD } from "../map/world";
import type { TransportMode } from "./api/client";

export interface ProvenanceField {
  readonly label: string;
  readonly value: string;
  readonly hint: string;
  /** Long machine values get a stable short form plus the full text in `title`. */
  readonly full?: string;
}

export function shortHash(hash: string, keep = 12): string {
  const body = hash.startsWith("sha256:") ? hash.slice(7) : hash;
  const prefix = hash.startsWith("sha256:") ? "sha256:" : "";
  return body.length <= keep ? hash : `${prefix}${body.slice(0, keep)}…${body.slice(-6)}`;
}

export function formatDate(iso: string | undefined, withTime = true): string {
  if (!iso) {
    return "not stated";
  }
  const stamp = Date.parse(iso);
  if (Number.isNaN(stamp)) {
    return iso;
  }
  const date = new Date(stamp);
  const ymd = date.toISOString().slice(0, 10);
  if (!withTime) {
    return ymd;
  }
  return `${ymd} ${date.toISOString().slice(11, 16)}Z`;
}

export function ageOf(iso: string | undefined, now = new Date()): string {
  if (!iso) {
    return "unknown age";
  }
  const stamp = Date.parse(iso);
  if (Number.isNaN(stamp)) {
    return "unknown age";
  }
  const seconds = Math.round((now.getTime() - stamp) / 1000);
  if (seconds < 0) {
    return `${formatDate(iso)} (from the future: a fixture clock, not a live feed)`;
  }
  const days = Math.floor(seconds / 86_400);
  if (days >= 1) {
    return `${formatDate(iso)} - ${days} day${days === 1 ? "" : "s"} old`;
  }
  return `${formatDate(iso)} - ${Math.max(0, Math.round(seconds / 60))} min old`;
}

/** The strip content for one screen: where this data came from, when, and its digest. */
export function provenanceFields(
  transport: { mode: TransportMode; label: string },
  lastReceivedAt?: string,
): ProvenanceField[] {
  const p = WORLD.provenance;
  const syntheticTransport = transport.mode === "synthetic";
  return [
    {
      label: "scenario",
      value: p.scenario_id,
      hint: `Frozen scenario: ${p.title}. Seed ${p.seed}, policy ${p.policy_version}, ${p.horizon_minutes} minute horizon.`,
    },
    {
      label: "source",
      value: syntheticTransport
        ? "SYNTHETIC DEV FIXTURES - not a live API"
        : "live simulation API (/v1) over the dev proxy",
      hint: `Transport: ${transport.label}. ${
        syntheticTransport
          ? "Every value on this screen came from src/lib/api/mock.ts. Nothing here was measured."
          : "Values are read from the running control plane; the corridor and pad geometry is still the frozen synthetic bundle."
      }`,
    },
    {
      label: "retrieved",
      value: ageOf(p.dataset_retrieved_at),
      hint: `GIS snapshot ${p.dataset_id} retrieved ${p.dataset_retrieved_at}, produced ${p.dataset_produced_at}. OpenStreetMap extract ${p.boundary_extract_date}, licence ${p.licence}, ${p.attribution}. Weather trace ${p.weather.station}: retrieved ${p.weather.retrieved_at}, expires ${p.weather.expires_at} (${p.weather.freshness}).`,
      full: `${p.dataset_retrieved_at} / weather ${p.weather.retrieved_at}`,
    },
    {
      label: "hash",
      value: shortHash(p.bundle_manifest_sha256),
      hint: `sha256 over the sorted bundle file digests - reproduce with \`make replay\`. Corridor bundle ${shortHash(p.bundle_sha256)}, GIS config ${p.config_version} ${shortHash(p.config_sha256)}, boundary ${shortHash(p.boundary_hash)}.`,
      full: p.bundle_manifest_sha256,
    },
    {
      label: "authoritative",
      value: "false - synthetic model",
      hint: `${p.what_this_does_not_prove} Corridor basis: "${p.corridor_summary.basis}"; ${p.corridor_summary.what_this_does_not_prove.join("; ")}.`,
    },
    ...(lastReceivedAt
      ? [
          {
            label: "response",
            value: ageOf(lastReceivedAt),
            hint: "When this screen last read the API.",
            full: lastReceivedAt,
          },
        ]
      : []),
  ];
}
