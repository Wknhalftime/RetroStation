import { useEffect, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";
import {
  boxHit,
  markerAt,
  moveView,
  msAt,
  panView,
  resizeEdge,
  zoomView,
  type MarkerKey,
  type Markers,
  type View,
} from "./cueMath";
import { drawWaveform, type LabelRect } from "./drawWaveform";

// One waveform view with draggable markers. Prop-driven: it is told the peaks, the view and the
// markers, and reports what the pointer asks for (a marker moved, the view zoomed or panned, a
// close-up box moved). It fetches nothing and knows nothing of files, so station liners can
// reuse it.

export type BoxKey = "start" | "end";

export interface WaveformPaneProps {
  peaks: Uint8Array;
  perSecond: number;
  /** The timeline's length (views and markers stay inside it); peaks past their end draw empty. */
  durationMs: number;
  view: View;
  markers: Markers;
  playheadMs: number | null;
  labels: boolean;
  /** Close-up windows to draw and drag; only for a pane whose view is the whole timeline. */
  boxes?: { start: View; end: View };
  onMarker(key: MarkerKey, ms: number): void;
  /** Set on a close-up: the wheel zooms and pans it, and dragging empty space pans it. */
  onView?(v: View): void;
  onBox?(which: BoxKey, v: View): void;
  height?: number;
  label?: string;
}

type Drag =
  | { kind: "marker"; key: MarkerKey }
  | { kind: "pan"; startClientX: number; startView: View }
  | { kind: "move"; which: BoxKey; grabMs: number; startBox: View }
  | { kind: "resize"; which: BoxKey; edge: "from" | "to" };

const ZOOM_IN = 0.8;
const ZOOM_OUT = 1.25;
const PAN_FRACTION = 0.1;
const BOX_ORDER: readonly BoxKey[] = ["end", "start"];

/** Where the pointer is across the canvas; null while the canvas has no width (msAt is NaN). */
function pointerAt(
  e: { clientX: number; clientY: number },
  canvas: HTMLCanvasElement
): { x: number; y: number; width: number } | null {
  const rect = canvas.getBoundingClientRect();
  if (!(rect.width > 0)) return null;
  return { x: e.clientX - rect.left, y: e.clientY - rect.top, width: rect.width };
}

function labelAt(x: number, y: number, rects: readonly LabelRect[]): MarkerKey | null {
  const hit = rects.find((r) => x >= r.x && x <= r.x + r.w && y >= r.y && y <= r.y + r.h);
  return hit ? hit.key : null;
}

function centre(v: View): number {
  return (v.from + v.to) / 2;
}

export function WaveformPane(props: WaveformPaneProps) {
  const { peaks, perSecond, view, markers, playheadMs, labels, boxes, height = 130 } = props;
  const containerRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const labelRects = useRef<LabelRect[]>([]);
  const drag = useRef<Drag | null>(null);
  const latest = useRef(props);
  const [width, setWidth] = useState(0);

  useEffect(() => {
    latest.current = props;
  });

  // Follow the container's width (the canvas is drawn at device pixels for that width).
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    setWidth(container.clientWidth);
    if (typeof ResizeObserver !== "function") return;
    const observer = new ResizeObserver((entries) => {
      const entry = entries[0];
      if (entry) setWidth(entry.contentRect.width);
    });
    observer.observe(container);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || width <= 0) return;
    const g = canvas.getContext("2d");
    if (!g) return;
    const ratio = window.devicePixelRatio || 1;
    canvas.width = Math.round(width * ratio);
    canvas.height = Math.round(height * ratio);
    g.setTransform(ratio, 0, 0, ratio, 0, 0);
    labelRects.current = drawWaveform(g, width, height, {
      peaks,
      perSecond,
      view,
      markers,
      playheadMs,
      labels,
      boxes,
    });
  }, [peaks, perSecond, view, markers, playheadMs, labels, boxes, width, height]);

  // The wheel needs a non-passive listener to stop the page scrolling; React's is passive.
  const zoomable = props.onView !== undefined;
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || !zoomable) return;
    const onWheel = (e: WheelEvent) => {
      const { view: current, durationMs, onView } = latest.current;
      const at = pointerAt(e, canvas);
      if (!onView || !at) return;
      e.preventDefault();
      // Shift + wheel arrives as deltaX in some browsers.
      const delta = e.deltaY !== 0 ? e.deltaY : e.deltaX;
      if (delta === 0) return;
      const pan = e.shiftKey || Math.abs(e.deltaX) > Math.abs(e.deltaY);
      const next = pan
        ? panView(
            current,
            Math.sign(delta) * (current.to - current.from) * PAN_FRACTION,
            durationMs
          )
        : zoomView(
            current,
            msAt(at.x, at.width, current),
            delta > 0 ? ZOOM_OUT : ZOOM_IN,
            durationMs
          );
      // Several wheel events can land before the next render: build on the view just asked for.
      latest.current = { ...latest.current, view: next };
      onView(next);
    };
    canvas.addEventListener("wheel", onWheel, { passive: false });
    return () => canvas.removeEventListener("wheel", onWheel);
  }, [zoomable]);

  function startDrag(e: ReactPointerEvent<HTMLCanvasElement>) {
    const canvas = e.currentTarget;
    const at = pointerAt(e, canvas);
    if (!at || e.button !== 0) return;
    const { durationMs, onBox, onView } = props;
    const ms = msAt(at.x, at.width, view);
    const key = labelAt(at.x, at.y, labelRects.current) ?? markerAt(at.x, at.width, view, markers);
    let next: Drag | null = null;
    if (key) {
      next = { kind: "marker", key };
    } else if (boxes && onBox) {
      next = boxDrag(at.x, at.width, ms, boxes, durationMs, onBox);
    } else if (onView) {
      next = { kind: "pan", startClientX: e.clientX, startView: view };
    }
    if (!next) return;
    drag.current = next;
    if (typeof canvas.setPointerCapture === "function") canvas.setPointerCapture(e.pointerId);
  }

  function continueDrag(e: ReactPointerEvent<HTMLCanvasElement>) {
    const canvas = e.currentTarget;
    const at = pointerAt(e, canvas);
    if (!at) return;
    const d = drag.current;
    if (!d) {
      canvas.style.cursor = cursorAt(at.x, at.y, at.width);
      return;
    }
    const { durationMs, onMarker, onView, onBox } = props;
    const ms = msAt(at.x, at.width, view);
    if (!Number.isFinite(ms)) return;
    if (d.kind === "marker") {
      onMarker(d.key, ms);
    } else if (d.kind === "pan") {
      const span = d.startView.to - d.startView.from;
      const deltaMs = -((e.clientX - d.startClientX) / at.width) * span;
      onView?.(panView(d.startView, deltaMs, durationMs));
    } else if (d.kind === "move") {
      onBox?.(d.which, moveView(d.startBox, d.startBox.from + (ms - d.grabMs), durationMs));
    } else if (boxes) {
      onBox?.(d.which, resizeEdge(boxes[d.which], d.edge, ms, durationMs));
    }
  }

  function endDrag() {
    drag.current = null;
  }

  function cursorAt(x: number, y: number, w: number): string {
    if (labelAt(x, y, labelRects.current) ?? markerAt(x, w, view, markers)) return "ew-resize";
    if (boxes) {
      for (const which of BOX_ORDER) {
        const hit = boxHit(x, w, props.durationMs, boxes[which]);
        if (hit === "from" || hit === "to") return "ew-resize";
        if (hit === "body") return "grab";
      }
      return "pointer";
    }
    return props.onView ? "grab" : "default";
  }

  return (
    <div
      ref={containerRef}
      className="relative overflow-hidden rounded-md bg-[#14181f]"
      style={{ height }}
    >
      <canvas
        ref={canvasRef}
        aria-label={props.label}
        className="absolute inset-0 h-full w-full touch-none select-none"
        onPointerDown={startDrag}
        onPointerMove={continueDrag}
        onPointerUp={endDrag}
        onPointerCancel={endDrag}
      />
    </div>
  );
}

/**
 * A press on the whole-file pane: an edge resizes the box under it, the middle moves it, and an
 * empty spot brings the nearer box there (centred) and keeps moving it while the button is held.
 */
function boxDrag(
  x: number,
  width: number,
  ms: number,
  boxes: { start: View; end: View },
  durationMs: number,
  onBox: (which: BoxKey, v: View) => void
): Drag {
  for (const which of BOX_ORDER) {
    const hit = boxHit(x, width, durationMs, boxes[which]);
    if (hit === "from" || hit === "to") return { kind: "resize", which, edge: hit };
    if (hit === "body") return { kind: "move", which, grabMs: ms, startBox: boxes[which] };
  }
  const which: BoxKey =
    Math.abs(ms - centre(boxes.start)) < Math.abs(ms - centre(boxes.end)) ? "start" : "end";
  const box = boxes[which];
  const moved = moveView(box, ms - (box.to - box.from) / 2, durationMs);
  onBox(which, moved);
  return { kind: "move", which, grabMs: ms, startBox: moved };
}
