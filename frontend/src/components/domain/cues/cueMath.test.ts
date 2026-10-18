// Cue editor PR 3, Task 9: the pure math behind the editor (cueMath.ts), node environment.
// Traceability: R1 (the ordering the editor enforces: Start < EOM <= Stop, Start <= Fade-out
// <= Stop, Start < Stop, all inside the file), R8 (the End preview starts at the left edge of
// the End view, never before Start; the Start preview runs Start..Stop), C1 (the fade runs from
// Fade-out to silence exactly at Stop), Review Focus 5 (nothing is pushed: moving one marker
// never moves another).
// Values are worked out by hand from the interface text in the Task 9 brief.
import { describe, it, expect } from "vitest";
import {
  END_VIEW_AFTER_MS,
  END_VIEW_BEFORE_MS,
  HIT_PX,
  MIN_SPAN_MS,
  START_VIEW_MS,
  boxHit,
  defaultViews,
  formatMs,
  fromApi,
  gainAt,
  markerAt,
  moveView,
  msAt,
  nudge,
  panView,
  peaksForView,
  placeMarker,
  previewRange,
  resizeEdge,
  sameMarkers,
  toApi,
  xAt,
  zoomView,
  type MarkerKey,
  type Markers,
  type View,
} from "./cueMath";

// The Devil You Know as the brief gives it: a 286.280 s file, the first stop at 4:31.800.
const M: Markers = Object.freeze({ start: 0, eom: 271_000, fadeAt: 270_500, stop: 271_800 });
const D = 286_280;
const KEYS: MarkerKey[] = ["start", "eom", "fadeAt", "stop"];

describe("constants", () => {
  it("exports the brief's values", () => {
    expect(MIN_SPAN_MS).toBe(200);
    expect(START_VIEW_MS).toBe(15_000);
    expect(END_VIEW_BEFORE_MS).toBe(12_000);
    expect(END_VIEW_AFTER_MS).toBe(1_000);
    expect(HIT_PX).toBe(6);
  });
});

describe("placeMarker (R1, Review Focus 5)", () => {
  it("clamps eom into (start, stop]", () => {
    expect(placeMarker(M, "eom", 300_000, D).eom).toBe(271_800);
    expect(placeMarker(M, "eom", -5, D).eom).toBe(1);
  });

  it("keeps stop at or after eom and fadeAt and within the file", () => {
    expect(placeMarker(M, "stop", 100, D).stop).toBe(271_000);
    expect(placeMarker(M, "stop", 999_999, D).stop).toBe(D);
  });

  it("holds stop at the later of eom and fadeAt, whichever that is", () => {
    const fadeLater: Markers = { start: 0, eom: 1_000, fadeAt: 5_000, stop: 6_000 };
    expect(placeMarker(fadeLater, "stop", 100, D).stop).toBe(5_000);
  });

  it("clamps fadeAt into [start, stop]", () => {
    expect(placeMarker(M, "fadeAt", -50, D).fadeAt).toBe(0);
    expect(placeMarker(M, "fadeAt", 999_999, D).fadeAt).toBe(271_800);
    const raised = { ...M, start: 1_000 };
    expect(placeMarker(raised, "fadeAt", 10, D).fadeAt).toBe(1_000);
  });

  it("lets eom and fadeAt sit in either order relative to each other", () => {
    expect(placeMarker(M, "fadeAt", 271_500, D).fadeAt).toBe(271_500);
    expect(placeMarker(M, "eom", 270_000, D).eom).toBe(270_000);
  });

  it("clamps start below eom - 1, fadeAt and stop - 1", () => {
    expect(placeMarker(M, "start", 999_999, D).start).toBe(270_500);
    expect(placeMarker(M, "start", -10, D).start).toBe(0);
    const tight: Markers = { start: 0, eom: 100, fadeAt: 100, stop: 200 };
    expect(placeMarker(tight, "start", 150, D).start).toBe(99);
    const eomLowest: Markers = { start: 0, eom: 50, fadeAt: 80, stop: 200 };
    expect(placeMarker(eomLowest, "start", 150, D).start).toBe(49);
  });

  it("rounds to whole ms", () => {
    expect(placeMarker(M, "fadeAt", 270_600.4, D).fadeAt).toBe(270_600);
    expect(placeMarker(M, "fadeAt", 270_600.6, D).fadeAt).toBe(270_601);
  });

  it("never pushes the other three markers (Review Focus 5)", () => {
    expect(placeMarker(M, "eom", 300_000, D)).toEqual({ ...M, eom: 271_800 });
    expect(placeMarker(M, "stop", 100, D)).toEqual({ ...M, stop: 271_000 });
    for (const key of KEYS) {
      const out = placeMarker(M, key, 123_456, D);
      for (const other of KEYS.filter((k) => k !== key)) {
        expect(out[other]).toBe(M[other]);
      }
    }
  });

  it("returns a new object and leaves its input alone", () => {
    const out = placeMarker(M, "eom", 270_000, D);
    expect(out).not.toBe(M);
    expect(M.eom).toBe(271_000);
  });
});

describe("nudge", () => {
  it("is placeMarker at the marker's own position plus the delta", () => {
    expect(nudge(M, "eom", -10, D).eom).toBe(270_990);
    expect(nudge(M, "fadeAt", 100, D).fadeAt).toBe(270_600);
    expect(nudge(M, "start", 1_000, D).start).toBe(1_000);
  });

  it("clamps the way placeMarker does", () => {
    expect(nudge(M, "eom", 1_000, D).eom).toBe(271_800);
    expect(nudge(M, "stop", 100_000, D).stop).toBe(D);
    expect(nudge(M, "stop", -10_000, D).stop).toBe(271_000);
    expect(nudge(M, "start", -1_000, D).start).toBe(0);
  });

  it("moves only the nudged marker", () => {
    expect(nudge(M, "eom", -10, D)).toEqual({ ...M, eom: 270_990 });
  });
});

describe("sameMarkers", () => {
  it("is true for equal values, even from different objects", () => {
    expect(sameMarkers(M, { ...M })).toBe(true);
  });

  it("is false when any one marker differs", () => {
    for (const key of KEYS) {
      expect(sameMarkers(M, { ...M, [key]: M[key] + 1 })).toBe(false);
    }
  });
});

describe("defaultViews", () => {
  it("opens the Start view on the first 15 s and the End view on 12 s before Stop", () => {
    expect(defaultViews(M, D)).toEqual({
      start: { from: 0, to: 15_000 },
      end: { from: 259_800, to: 272_800 },
    });
  });

  it("fits a file shorter than the windows", () => {
    const short: Markers = { start: 0, eom: 8_000, fadeAt: 8_000, stop: 9_000 };
    expect(defaultViews(short, 10_000)).toEqual({
      start: { from: 0, to: 10_000 },
      end: { from: 0, to: 10_000 },
    });
  });

  it("does not run the End view past the end of the file", () => {
    const toTheEnd: Markers = { start: 0, eom: D, fadeAt: D - 4_000, stop: D };
    expect(defaultViews(toTheEnd, D).end).toEqual({ from: D - 12_000, to: D });
  });
});

describe("zoomView", () => {
  it("keeps the point under the mouse", () => {
    expect(zoomView({ from: 0, to: 10_000 }, 2_500, 0.5, D)).toEqual({ from: 1_250, to: 6_250 });
  });

  it("zooms out around the point too", () => {
    expect(zoomView({ from: 0, to: 10_000 }, 0, 2, D)).toEqual({ from: 0, to: 20_000 });
    expect(zoomView({ from: 276_280, to: 286_280 }, D, 2, D)).toEqual({
      from: 266_280,
      to: D,
    });
  });

  it("keeps a point at the right edge at the right edge when zooming in", () => {
    expect(zoomView({ from: 276_280, to: 286_280 }, D, 0.5, D)).toEqual({
      from: 281_280,
      to: D,
    });
  });

  it("clamps at both ends", () => {
    expect(zoomView({ from: 0, to: 10_000 }, 0, 1e6, D)).toEqual({ from: 0, to: D });
    const v = zoomView({ from: 0, to: 300 }, 100, 0.01, D);
    // 200 ms is the smallest span; allow for floating-point residue in from/to.
    expect(v.to - v.from).toBeCloseTo(200, 6);
  });
});

describe("zoomView stays inside the file (Review Focus 5)", () => {
  it("when the zoomed view cannot keep the point's fraction at either end", () => {
    const cases: [View, number][] = [
      // Zooming out around the middle would start at -4 000.
      [zoomView({ from: 1_000, to: 11_000 }, 6_000, 2, D), 6_000],
      // Zooming out near the end would finish at 294 000, past the file.
      [zoomView({ from: 275_000, to: 285_000 }, 276_000, 2, D), 276_000],
    ];
    for (const [v, at] of cases) {
      expect(v.from).toBeGreaterThanOrEqual(0);
      expect(v.to).toBeLessThanOrEqual(D);
      expect(v.to - v.from).toBeGreaterThanOrEqual(MIN_SPAN_MS - 1e-6);
      expect(v.to - v.from).toBeLessThanOrEqual(D);
      // Whether it shifts or clips, the point being zoomed on stays in view.
      expect(v.from).toBeLessThanOrEqual(at);
      expect(v.to).toBeGreaterThanOrEqual(at);
    }
  });
});

describe("panView", () => {
  it("moves the view by the delta and keeps its span", () => {
    expect(panView({ from: 1_000, to: 6_000 }, 500, D)).toEqual({ from: 1_500, to: 6_500 });
  });

  it("stops at the file's start", () => {
    expect(panView({ from: 1_000, to: 6_000 }, -5_000, D)).toEqual({ from: 0, to: 5_000 });
  });

  it("stops at the file's end", () => {
    expect(panView({ from: 280_000, to: 285_000 }, 10_000, D)).toEqual({
      from: 281_280,
      to: D,
    });
  });
});

describe("moveView", () => {
  it("puts the view at the new left edge and keeps its span", () => {
    expect(moveView({ from: 1_000, to: 5_000 }, 2_000, D)).toEqual({ from: 2_000, to: 6_000 });
  });

  it("stops at both ends of the file", () => {
    expect(moveView({ from: 1_000, to: 5_000 }, -50, D)).toEqual({ from: 0, to: 4_000 });
    expect(moveView({ from: 1_000, to: 5_000 }, 999_999, D)).toEqual({
      from: D - 4_000,
      to: D,
    });
  });
});

describe("resizeEdge", () => {
  it("moves the named edge", () => {
    expect(resizeEdge({ from: 1_000, to: 5_000 }, "from", 2_000, D)).toEqual({
      from: 2_000,
      to: 5_000,
    });
    expect(resizeEdge({ from: 1_000, to: 5_000 }, "to", 6_000, D)).toEqual({
      from: 1_000,
      to: 6_000,
    });
  });

  it("keeps a minimum span", () => {
    expect(resizeEdge({ from: 1_000, to: 5_000 }, "from", 4_950, D)).toEqual({
      from: 4_800,
      to: 5_000,
    });
    expect(resizeEdge({ from: 1_000, to: 5_000 }, "to", 1_100, D)).toEqual({
      from: 1_000,
      to: 1_200,
    });
  });

  it("stays inside the file", () => {
    expect(resizeEdge({ from: 1_000, to: 5_000 }, "from", -100, D)).toEqual({
      from: 0,
      to: 5_000,
    });
    expect(resizeEdge({ from: 1_000, to: 5_000 }, "to", 999_999, D)).toEqual({
      from: 1_000,
      to: D,
    });
  });
});

describe("msAt and xAt", () => {
  const v: View = { from: 1_000, to: 3_000 };

  it("map pixels to milliseconds across the view", () => {
    expect(msAt(0, 100, v)).toBe(1_000);
    expect(msAt(50, 100, v)).toBe(2_000);
    expect(msAt(100, 100, v)).toBe(3_000);
  });

  it("map milliseconds to pixels across the view", () => {
    expect(xAt(1_000, 100, v)).toBe(0);
    expect(xAt(1_500, 100, v)).toBe(25);
    expect(xAt(3_000, 100, v)).toBe(100);
  });

  it("are inverses", () => {
    expect(xAt(msAt(40, 200, v), 200, v)).toBeCloseTo(40, 9);
  });
});

describe("markerAt", () => {
  // One pixel per millisecond, so a distance in px is a distance in ms.
  const v: View = { from: 0, to: 1_000 };
  const m: Markers = { start: 100, eom: 500, fadeAt: 504, stop: 800 };

  it("finds the marker under the pointer", () => {
    expect(markerAt(100, 1_000, v, m)).toBe("start");
    expect(markerAt(800, 1_000, v, m)).toBe("stop");
  });

  it("finds the nearest of two close markers", () => {
    expect(markerAt(499, 1_000, v, m)).toBe("eom");
    expect(markerAt(503, 1_000, v, m)).toBe("fadeAt");
  });

  it("hits within 6 px and misses beyond it", () => {
    expect(markerAt(95, 1_000, v, m)).toBe("start");
    expect(markerAt(105, 1_000, v, m)).toBe("start");
    expect(markerAt(93, 1_000, v, m)).toBeNull();
    expect(markerAt(107, 1_000, v, m)).toBeNull();
    expect(markerAt(300, 1_000, v, m)).toBeNull();
  });

  it("measures the distance in pixels, not milliseconds", () => {
    // 10 px per ms: the Start marker is at px 0 and the Stop marker (100 ms) at px 1000.
    const zoomed: View = { from: 0, to: 100 };
    const only: Markers = { start: 0, eom: 50, fadeAt: 50, stop: 100 };
    expect(markerAt(5, 1_000, zoomed, only)).toBe("start");
    expect(markerAt(7, 1_000, zoomed, only)).toBeNull();
    expect(markerAt(996, 1_000, zoomed, only)).toBe("stop");
  });

  it("answers one of the two when EOM and Fade-out sit together (the automatic opening)", () => {
    const together: Markers = { start: 0, eom: 500, fadeAt: 500, stop: 800 };
    expect(["eom", "fadeAt"]).toContain(markerAt(500, 1_000, v, together));
  });

  it("ignores markers outside the view", () => {
    expect(markerAt(5, 1_000, { from: 600, to: 700 }, m)).toBeNull();
  });
});

describe("boxHit", () => {
  // The whole-song strip: 10 ms per pixel, the box at px 200..600.
  const box: View = { from: 2_000, to: 6_000 };

  it("hits an edge within 6 px of it", () => {
    expect(boxHit(203, 1_000, 10_000, box)).toBe("from");
    expect(boxHit(195, 1_000, 10_000, box)).toBe("from");
    expect(boxHit(597, 1_000, 10_000, box)).toBe("to");
    expect(boxHit(605, 1_000, 10_000, box)).toBe("to");
  });

  it("hits the body between the edges", () => {
    expect(boxHit(400, 1_000, 10_000, box)).toBe("body");
  });

  it("misses outside the box and beyond the edge margin", () => {
    expect(boxHit(100, 1_000, 10_000, box)).toBeNull();
    expect(boxHit(190, 1_000, 10_000, box)).toBeNull();
    expect(boxHit(800, 1_000, 10_000, box)).toBeNull();
  });

  it("lets an edge win over the body on a box narrower than the margins", () => {
    const narrow: View = { from: 2_000, to: 2_100 };
    expect(boxHit(200, 1_000, 10_000, narrow)).toBe("from");
    expect(boxHit(210, 1_000, 10_000, narrow)).toBe("to");
  });
});

describe("previewRange (R8, Lance's ruling)", () => {
  it("start runs from Start to Stop", () => {
    expect(previewRange("start", M, { from: 268_000, to: 272_000 })).toEqual({
      from: 0,
      to: 271_800,
    });
  });

  it("end starts at the End view's left edge and stops at Stop", () => {
    expect(previewRange("end", M, { from: 268_000, to: 272_000 })).toEqual({
      from: 268_000,
      to: 271_800,
    });
  });

  it("end never starts before Start", () => {
    expect(previewRange("end", { ...M, start: 269_000 }, { from: 268_000, to: 272_000 }).from).toBe(
      269_000
    );
  });
});

describe("gainAt (C1)", () => {
  it("is 1 until Fade-out", () => {
    expect(gainAt(0, M)).toBe(1);
    expect(gainAt(270_500, M)).toBe(1);
  });

  it("ramps linearly from Fade-out to 0 exactly at Stop", () => {
    expect(gainAt(270_825, M)).toBeCloseTo(0.75);
    expect(gainAt(271_150, M)).toBeCloseTo(0.5);
    expect(gainAt(271_475, M)).toBeCloseTo(0.25);
    expect(gainAt(271_800, M)).toBe(0);
  });

  it("is 0 after Stop", () => {
    expect(gainAt(271_801, M)).toBe(0);
    expect(gainAt(D, M)).toBe(0);
  });

  it("copes with a fade of zero length (Fade-out at Stop)", () => {
    const abrupt: Markers = { ...M, fadeAt: 271_800 };
    expect(gainAt(271_799, abrupt)).toBe(1);
    expect(Number.isFinite(gainAt(271_800, abrupt))).toBe(true);
    expect(gainAt(271_801, abrupt)).toBe(0);
  });
});

describe("peaksForView", () => {
  // Four buckets at 4 per second: the first second of the song, 250 ms each.
  const peaks = Uint8Array.from([1, 9, 3, 4]);

  it("takes the max per column", () => {
    const out = peaksForView(peaks, 4, { from: 0, to: 1_000 }, 2);
    expect(Array.from(out)).toEqual([9, 4]);
  });

  it("answers a Uint8Array with one value per pixel column", () => {
    const out = peaksForView(peaks, 4, { from: 0, to: 1_000 }, 4);
    expect(out).toBeInstanceOf(Uint8Array);
    expect(out.length).toBe(4);
    expect(Array.from(out)).toEqual([1, 9, 3, 4]);
  });

  it("reads only the buckets inside the view", () => {
    expect(Array.from(peaksForView(peaks, 4, { from: 500, to: 1_000 }, 2))).toEqual([3, 4]);
  });

  it("draws columns past the last peak empty (ruling)", () => {
    // 1 000 ms of peaks; the views run to 1 500 ms. The old "nearest bucket" wording would read
    // the last bucket (4) for the columns past it.
    expect(Array.from(peaksForView(peaks, 4, { from: 500, to: 1_500 }, 4))).toEqual([3, 4, 0, 0]);
    expect(Array.from(peaksForView(peaks, 4, { from: 0, to: 1_500 }, 3))).toEqual([9, 4, 0]);
  });

  it("fills columns narrower than a bucket from their nearest bucket", () => {
    expect(Array.from(peaksForView(peaks, 4, { from: 0, to: 1_000 }, 8))).toEqual([
      1, 1, 9, 9, 3, 3, 4, 4,
    ]);
    expect(Array.from(peaksForView(peaks, 4, { from: 250, to: 500 }, 2))).toEqual([9, 9]);
  });
});

describe("formatMs", () => {
  it("writes m:ss.mmm", () => {
    expect(formatMs(271_800)).toBe("4:31.800");
    expect(formatMs(241_100)).toBe("4:01.100");
    expect(formatMs(247_100)).toBe("4:07.100");
    expect(formatMs(0)).toBe("0:00.000");
    expect(formatMs(1)).toBe("0:00.001");
    expect(formatMs(59_999)).toBe("0:59.999");
    expect(formatMs(60_000)).toBe("1:00.000");
  });
});

describe("fromApi and toApi", () => {
  it("round trip", () => {
    expect(fromApi(toApi(M))).toEqual(M);
  });

  it("translate between the editor's names and the API's", () => {
    expect(toApi(M)).toEqual({
      start_ms: 0,
      eom_ms: 271_000,
      fade_at_ms: 270_500,
      stop_ms: 271_800,
    });
    expect(fromApi({ start_ms: 1, eom_ms: 2, fade_at_ms: 3, stop_ms: 4 })).toEqual({
      start: 1,
      eom: 2,
      fadeAt: 3,
      stop: 4,
    });
  });
});
