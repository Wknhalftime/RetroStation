import {
  formatMs,
  gainAt,
  msAt,
  peaksForView,
  xAt,
  type MarkerKey,
  type Markers,
  type View,
} from "./cueMath";
import { MARKER_COLOR, MARKER_LABEL, MARKER_ORDER } from "./markerStyle";

// Paints one waveform pane on a 2D canvas context. Pure drawing: no React, no events. Returns
// where the marker labels landed, so the pane can let a label be grabbed (markers that sit on the
// same spot, such as EOM and Fade-out, are told apart by their labels).

export interface PaneScene {
  peaks: Uint8Array;
  perSecond: number;
  view: View;
  markers: Markers;
  playheadMs: number | null;
  labels: boolean;
  /** Close-up windows drawn as boxes; only on a pane whose view is the whole file. */
  boxes?: { start: View; end: View };
}

export interface LabelRect {
  key: MarkerKey;
  x: number;
  y: number;
  w: number;
  h: number;
}

const BACKGROUND = "#14181f";
const OUTSIDE = "#30353d";
const INSIDE_FULL = "#3c4f68";
const INSIDE_GAIN = "#7fa7d8";
const GRID = "rgba(255,255,255,.45)";
const LABEL_HEIGHT = 14;
const LABEL_ROW = 16;

const GRID_STEPS_MS = [10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10_000, 30_000, 60_000];
const RULER_STEPS_MS = [10_000, 15_000, 30_000, 60_000, 120_000, 300_000, 600_000];

/** `m:ss.mmm` without trailing zeros: 4:01.100 → 4:01.1, 4:10.000 → 4:10. */
function shortTime(ms: number): string {
  return formatMs(ms).replace(/\.?0+$/, "");
}

function drawPeaks(g: CanvasRenderingContext2D, w: number, h: number, s: PaneScene): void {
  const columns = peaksForView(s.peaks, s.perSecond, s.view, w);
  const mid = h / 2;
  for (let x = 0; x < columns.length; x++) {
    const half = (columns[x] / 255) * h * 0.45;
    if (half <= 0) continue;
    const t = msAt(x + 0.5, w, s.view);
    const outside = t < s.markers.start || t > s.markers.stop;
    g.fillStyle = outside ? OUTSIDE : INSIDE_FULL;
    g.fillRect(x, mid - half, 1, 2 * half);
    if (outside) continue;
    // Shrinks along the fade, so the fade's shape is visible (C1: silent exactly at Stop).
    const scaled = half * gainAt(t, s.markers);
    g.fillStyle = INSIDE_GAIN;
    g.fillRect(x, mid - scaled, 1, 2 * scaled);
  }
}

function drawFadeLine(g: CanvasRenderingContext2D, w: number, h: number, s: PaneScene): void {
  const { fadeAt, stop } = s.markers;
  if (stop <= fadeAt) return;
  g.strokeStyle = MARKER_COLOR.fadeAt;
  g.setLineDash([4, 3]);
  g.lineWidth = 1.5;
  g.beginPath();
  g.moveTo(xAt(fadeAt, w, s.view), 4);
  g.lineTo(xAt(stop, w, s.view), h - 4);
  g.stroke();
  g.setLineDash([]);
}

function drawBoxes(g: CanvasRenderingContext2D, w: number, h: number, s: PaneScene): void {
  if (!s.boxes) return;
  const named: [View, string][] = [
    [s.boxes.start, "Start view"],
    [s.boxes.end, "End view"],
  ];
  g.font = "11px sans-serif";
  for (const [box, name] of named) {
    const left = xAt(box.from, w, s.view);
    const width = Math.max(4, xAt(box.to, w, s.view) - left);
    g.fillStyle = "rgba(255,255,255,.10)";
    g.fillRect(left, 0, width, h);
    g.strokeStyle = "rgba(255,255,255,.75)";
    g.lineWidth = 1;
    g.strokeRect(left + 0.5, 0.5, width - 1, h - 1);
    g.fillStyle = "rgba(255,255,255,.9)";
    g.fillRect(left, 0, 3, h);
    g.fillRect(left + width - 3, 0, 3, h);
    const textWidth = g.measureText(name).width;
    const lx = Math.min(Math.max(2, left + width / 2 - textWidth / 2), w - textWidth - 2);
    g.fillStyle = "rgba(0,0,0,.7)";
    g.fillRect(lx - 3, h - 16, textWidth + 6, 14);
    g.fillStyle = "#fff";
    g.fillText(name, lx, h - 5);
  }
}

/** Time ticks: a ruler along the top of a whole-file pane, a fine grid along a close-up's foot. */
function drawTimeGrid(g: CanvasRenderingContext2D, w: number, h: number, s: PaneScene): void {
  const span = s.view.to - s.view.from;
  const ruler = s.boxes !== undefined;
  const steps = ruler ? RULER_STEPS_MS : GRID_STEPS_MS;
  const step =
    steps.find((candidate) => span / candidate <= (ruler ? 12 : 10)) ?? steps[steps.length - 1];
  g.fillStyle = GRID;
  g.font = "10px monospace";
  for (let t = Math.ceil(s.view.from / step) * step; t <= s.view.to; t += step) {
    const x = xAt(t, w, s.view);
    if (ruler) {
      g.fillRect(x, 0, 1, 5);
      g.fillText(shortTime(t), x + 2, 12);
    } else {
      g.fillRect(x, h - 6, 1, 6);
      g.fillText(shortTime(t), x + 2, h - 8);
    }
  }
}

function drawMarkers(g: CanvasRenderingContext2D, w: number, h: number, s: PaneScene): LabelRect[] {
  const placed: LabelRect[] = [];
  let row = 0;
  g.font = "bold 10px sans-serif";
  for (const key of MARKER_ORDER) {
    const x = xAt(s.markers[key], w, s.view);
    if (x < -2 || x > w + 2) continue;
    g.fillStyle = MARKER_COLOR[key];
    g.fillRect(x - 1, 0, 2, h);
    if (!s.labels) continue;
    const label = MARKER_LABEL[key];
    const labelWidth = g.measureText(label).width + 8;
    const y = 4 + row * LABEL_ROW;
    row += 1;
    const lx = x + labelWidth + 4 > w ? x - labelWidth - 3 : x + 3;
    g.fillRect(lx, y, labelWidth, LABEL_HEIGHT);
    g.fillStyle = "#000";
    g.fillText(label, lx + 4, y + 11);
    placed.push({ key, x: lx, y, w: labelWidth, h: LABEL_HEIGHT });
  }
  return placed;
}

export function drawWaveform(
  g: CanvasRenderingContext2D,
  w: number,
  h: number,
  s: PaneScene
): LabelRect[] {
  g.fillStyle = BACKGROUND;
  g.fillRect(0, 0, w, h);
  drawPeaks(g, w, h, s);
  drawFadeLine(g, w, h, s);
  drawBoxes(g, w, h, s);
  drawTimeGrid(g, w, h, s);
  const labels = drawMarkers(g, w, h, s);
  if (s.playheadMs !== null) {
    g.fillStyle = "#fff";
    g.fillRect(xAt(s.playheadMs, w, s.view) - 1, 0, 2, h);
  }
  return labels;
}
