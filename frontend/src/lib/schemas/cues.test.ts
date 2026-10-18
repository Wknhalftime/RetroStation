// Cue editor PR 3, Task 10: the editor's wire schemas (traceability R7 API shapes as built in
// PR 2, backend/routers/library/cues.py; the "silent" analysis state is the PR 2 ruling I1).
import { describe, it, expect } from "vitest";
import {
  CueAnalysisSchema,
  CueEditorSchema,
  MarkersSchema,
  WaveformSchema,
  decodePeaks,
} from "./cues";

const automatic = { start_ms: 0, eom_ms: 241_100, fade_at_ms: 241_100, stop_ms: 247_100 };
const view = {
  file_id: "22222222-2222-4222-8222-222222222222",
  title: "Heretic Anthem",
  artist: "Slipknot",
  format: "flac",
  duration_ms: 250_000,
  analysis: "measured",
  automatic,
  manual: null,
};

describe("CueEditorSchema", () => {
  it("accepts the API's shape when there is no manual edit", () => {
    const parsed = CueEditorSchema.parse(view);
    expect(parsed.automatic).toEqual(automatic);
    expect(parsed.manual).toBeNull();
    expect(parsed.duration_ms).toBe(250_000);
    expect(parsed.format).toBe("flac");
  });

  it("accepts a manual edit", () => {
    const manual = { start_ms: 2_000, eom_ms: 240_000, fade_at_ms: 239_500, stop_ms: 246_000 };
    expect(CueEditorSchema.parse({ ...view, manual }).manual).toEqual(manual);
  });

  it("accepts a file without tags", () => {
    const parsed = CueEditorSchema.parse({ ...view, title: null, artist: null });
    expect(parsed.title).toBeNull();
    expect(parsed.artist).toBeNull();
  });

  it("rejects a missing automatic", () => {
    const { automatic: _dropped, ...without } = view;
    void _dropped;
    expect(() => CueEditorSchema.parse(without)).toThrow();
  });

  it("rejects an analysis state it does not know", () => {
    expect(() => CueEditorSchema.parse({ ...view, analysis: "bogus" })).toThrow();
  });

  it("rejects markers with a field missing", () => {
    const { fade_at_ms: _dropped, ...partial } = automatic;
    void _dropped;
    expect(() => CueEditorSchema.parse({ ...view, automatic: partial })).toThrow();
  });
});

describe("CueAnalysisSchema", () => {
  it.each(["measured", "failed", "waiting", "silent"])("accepts %s", (state) => {
    expect(CueAnalysisSchema.parse(state)).toBe(state);
  });

  it("rejects anything else", () => {
    expect(() => CueAnalysisSchema.parse("unknown")).toThrow();
  });
});

describe("MarkersSchema", () => {
  it("parses the API's four marker names", () => {
    expect(MarkersSchema.parse(automatic)).toEqual(automatic);
  });

  it("rejects a marker that is not a number", () => {
    expect(() => MarkersSchema.parse({ ...automatic, stop_ms: "247100" })).toThrow();
  });
});

describe("WaveformSchema", () => {
  it("accepts the API's shape", () => {
    const parsed = WaveformSchema.parse({ duration_ms: 250_000, per_second: 200, peaks: "AP8=" });
    expect(parsed.per_second).toBe(200);
    expect(parsed.peaks).toBe("AP8=");
  });

  it("rejects peaks that are not a string", () => {
    expect(() =>
      WaveformSchema.parse({ duration_ms: 250_000, per_second: 200, peaks: [0, 255] })
    ).toThrow();
  });

  it("rejects a missing peaks", () => {
    expect(() => WaveformSchema.parse({ duration_ms: 250_000, per_second: 200 })).toThrow();
  });
});

describe("decodePeaks", () => {
  it("decodes base64 to bytes", () => {
    const bytes = decodePeaks("AP8=");
    expect(bytes).toBeInstanceOf(Uint8Array);
    expect(Array.from(bytes)).toEqual([0, 255]);
  });

  it("decodes an empty string to no bytes", () => {
    expect(decodePeaks("").length).toBe(0);
  });

  it("round-trips every byte value", () => {
    const all = Uint8Array.from({ length: 256 }, (_unused, i) => i);
    const encoded = btoa(String.fromCharCode(...all));
    expect(Array.from(decodePeaks(encoded))).toEqual(Array.from(all));
  });

  it("decodes a padded run", () => {
    expect(Array.from(decodePeaks("AQIDBAU="))).toEqual([1, 2, 3, 4, 5]);
  });
});
