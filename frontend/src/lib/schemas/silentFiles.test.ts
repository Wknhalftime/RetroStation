import { describe, it, expect } from "vitest";
import { SilentFileReportSchema } from "./silentFiles";

const testimony = {
  file_id: "22222222-2222-4222-8222-222222222222",
  artist: "Mary J. Blige",
  title: "Testimony",
  album: "No More Drama",
  path: "D:\\Music\\No More Drama\\Mary J. Blige - Testimony.flac",
  duration_ms: 298666,
  reasons: ["low_bitrate", "short_cued_span"],
  bitrate_kbps: 17,
  cued_span_ms: 11700,
};
const report = { files: [testimony], silent: 0, nearly_silent: 1 };

describe("silent files schema", () => {
  it("parses the API's report", () => {
    expect(SilentFileReportSchema.parse(report).files[0].reasons).toEqual([
      "low_bitrate",
      "short_cued_span",
    ]);
  });

  it("rejects a reason it does not know", () => {
    const bad = { ...report, files: [{ ...testimony, reasons: ["quiet"] }] };
    expect(() => SilentFileReportSchema.parse(bad)).toThrow();
  });

  it("rejects a file listed for no reason", () => {
    const bad = { ...report, files: [{ ...testimony, reasons: [] }] };
    expect(() => SilentFileReportSchema.parse(bad)).toThrow();
  });

  it("accepts a file with no tags and no cue row", () => {
    const untagged = { ...testimony, artist: null, title: null, album: null, cued_span_ms: null };
    expect(SilentFileReportSchema.parse({ ...report, files: [untagged] }).files).toHaveLength(1);
  });
});
