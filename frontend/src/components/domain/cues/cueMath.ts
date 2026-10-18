// Pure math behind the cue editor: marker placement (R1), views and zoom, pixel hit-testing,
// preview ranges (R8), the fade gain (C1) and peak down-sampling. No fetch, no React, no DOM, and
// no knowledge of what is being edited, so station liners can reuse it. All times are in ms.

import type { ApiMarkers } from "@/lib/schemas/cues";

export type MarkerKey = "start" | "eom" | "fadeAt" | "stop";
export type Markers = { start: number; eom: number; fadeAt: number; stop: number };
export type View = { from: number; to: number };

export const MIN_SPAN_MS = 200;
export const START_VIEW_MS = 15_000;
export const END_VIEW_BEFORE_MS = 12_000;
export const END_VIEW_AFTER_MS = 1_000;
export const HIT_PX = 6;

const MARKER_KEYS: readonly MarkerKey[] = ["start", "eom", "fadeAt", "stop"];

function clamp(value: number, lo: number, hi: number): number {
  return Math.min(Math.max(value, lo), Math.max(lo, hi));
}

/**
 * Place one marker, rounded to whole ms, inside what R1 allows given the other three.
 * Never moves the others: start <= eom - 1, <= fadeAt, <= stop - 1; eom in [start + 1, stop];
 * fadeAt in [start, stop]; stop in [max(eom, fadeAt), duration].
 */
export function placeMarker(m: Markers, key: MarkerKey, ms: number, durationMs: number): Markers {
  const value = Math.round(ms);
  let placed: number;
  switch (key) {
    case "start":
      placed = clamp(value, 0, Math.min(m.eom - 1, m.fadeAt, m.stop - 1));
      break;
    case "eom":
      placed = clamp(value, m.start + 1, m.stop);
      break;
    case "fadeAt":
      placed = clamp(value, m.start, m.stop);
      break;
    case "stop":
      placed = clamp(value, Math.max(m.eom, m.fadeAt), durationMs);
      break;
  }
  return { ...m, [key]: placed };
}

export function nudge(m: Markers, key: MarkerKey, deltaMs: number, durationMs: number): Markers {
  return placeMarker(m, key, m[key] + deltaMs, durationMs);
}

export function sameMarkers(a: Markers, b: Markers): boolean {
  return MARKER_KEYS.every((key) => a[key] === b[key]);
}

/** The Start view opens on the first 15 s, the End view on the 12 s before Stop plus 1 s after. */
export function defaultViews(m: Markers, durationMs: number): { start: View; end: View } {
  return {
    start: { from: 0, to: Math.min(START_VIEW_MS, durationMs) },
    end: {
      from: Math.max(0, m.stop - END_VIEW_BEFORE_MS),
      to: Math.min(durationMs, m.stop + END_VIEW_AFTER_MS),
    },
  };
}

/** A view of the given span starting at `from`, shifted to lie inside [0, duration]. */
function fitView(from: number, span: number, durationMs: number): View {
  const start = clamp(from, 0, durationMs - span);
  return { from: start, to: start + span };
}

/** Zoom by `factor` (< 1 zooms in) keeping `atMs` at the same fraction of the view. */
export function zoomView(v: View, atMs: number, factor: number, durationMs: number): View {
  const span = v.to - v.from;
  const fraction = span > 0 ? (atMs - v.from) / span : 0;
  const newSpan = Math.min(Math.max(span * factor, MIN_SPAN_MS), durationMs);
  return fitView(atMs - fraction * newSpan, newSpan, durationMs);
}

export function panView(v: View, deltaMs: number, durationMs: number): View {
  return fitView(v.from + deltaMs, v.to - v.from, durationMs);
}

export function moveView(v: View, newFrom: number, durationMs: number): View {
  return fitView(newFrom, v.to - v.from, durationMs);
}

/** Move one edge of a view, keeping at least MIN_SPAN_MS and staying inside the file. */
export function resizeEdge(v: View, edge: "from" | "to", ms: number, durationMs: number): View {
  if (edge === "from") {
    return { from: clamp(ms, 0, v.to - MIN_SPAN_MS), to: v.to };
  }
  return { from: v.from, to: clamp(ms, v.from + MIN_SPAN_MS, durationMs) };
}

export function msAt(xPx: number, widthPx: number, v: View): number {
  return v.from + (xPx / widthPx) * (v.to - v.from);
}

export function xAt(ms: number, widthPx: number, v: View): number {
  return ((ms - v.from) / (v.to - v.from)) * widthPx;
}

/** The marker nearest the pointer within HIT_PX, or null. */
export function markerAt(xPx: number, widthPx: number, v: View, m: Markers): MarkerKey | null {
  let best: MarkerKey | null = null;
  let bestDistance = Infinity;
  for (const key of MARKER_KEYS) {
    const distance = Math.abs(xAt(m[key], widthPx, v) - xPx);
    if (distance <= HIT_PX && distance < bestDistance) {
      best = key;
      bestDistance = distance;
    }
  }
  return best;
}

/** Hit-test the zoom box drawn on the whole-song strip. An edge wins over the body. */
export function boxHit(
  xPx: number,
  widthPx: number,
  durationMs: number,
  box: View
): "from" | "to" | "body" | null {
  const left = (box.from / durationMs) * widthPx;
  const right = (box.to / durationMs) * widthPx;
  const toLeft = Math.abs(xPx - left);
  const toRight = Math.abs(xPx - right);
  if (toLeft <= HIT_PX && toLeft <= toRight) return "from";
  if (toRight <= HIT_PX) return "to";
  if (xPx > left && xPx < right) return "body";
  return null;
}

/** What each preview plays (R8): Start runs Start..Stop; End runs from the End view's left edge. */
export function previewRange(
  kind: "start" | "end",
  m: Markers,
  endView: View
): { from: number; to: number } {
  if (kind === "start") return { from: m.start, to: m.stop };
  return { from: Math.max(m.start, endView.from), to: m.stop };
}

/** Playback gain: 1 before Fade-out, linear to 0 exactly at Stop (C1), 0 after. */
export function gainAt(ms: number, m: Markers): number {
  if (ms >= m.stop) return 0;
  if (ms <= m.fadeAt) return 1;
  return 1 - (ms - m.fadeAt) / (m.stop - m.fadeAt);
}

/**
 * Down-sample peaks to one value per pixel column (the max of the buckets the column covers; a
 * column narrower than a bucket reads its nearest bucket). Columns past the last peak read 0.
 */
export function peaksForView(
  peaks: Uint8Array,
  perSecond: number,
  v: View,
  widthPx: number
): Uint8Array {
  const width = Math.max(0, Math.floor(widthPx));
  const out = new Uint8Array(width);
  const bucketsPerMs = perSecond / 1000;
  const columnMs = (v.to - v.from) / width;
  const epsilon = 1e-9;
  for (let column = 0; column < width; column++) {
    const first = Math.max(0, Math.floor((v.from + column * columnMs) * bucketsPerMs + epsilon));
    const end = Math.ceil((v.from + (column + 1) * columnMs) * bucketsPerMs - epsilon);
    const last = Math.min(peaks.length - 1, Math.max(first, end - 1));
    let max = 0;
    for (let i = first; i <= last; i++) {
      if (peaks[i] > max) max = peaks[i];
    }
    out[column] = max;
  }
  return out;
}

function pad(value: number, length: number): string {
  return String(value).padStart(length, "0");
}

/** `m:ss.mmm` */
export function formatMs(ms: number): string {
  const total = Math.max(0, Math.round(ms));
  const minutes = Math.floor(total / 60_000);
  const seconds = Math.floor((total % 60_000) / 1000);
  return `${minutes}:${pad(seconds, 2)}.${pad(total % 1000, 3)}`;
}

export function fromApi(a: ApiMarkers): Markers {
  return { start: a.start_ms, eom: a.eom_ms, fadeAt: a.fade_at_ms, stop: a.stop_ms };
}

export function toApi(m: Markers): ApiMarkers {
  return { start_ms: m.start, eom_ms: m.eom, fade_at_ms: m.fadeAt, stop_ms: m.stop };
}
