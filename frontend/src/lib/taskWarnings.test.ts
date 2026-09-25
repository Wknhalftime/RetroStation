import { describe, expect, it } from "vitest";
import { getWarningText } from "@/lib/taskWarnings";

describe("getWarningText", () => {
  it("returns null when there is no warning", () => {
    expect(getWarningText({ processed: 10 })).toBeNull();
  });

  it("returns null when the warning is not a string", () => {
    expect(getWarningText({ warning: 42 })).toBeNull();
  });

  it("renders the library scan warning that already existed", () => {
    expect(getWarningText({ warning: "no_audio_files_found" })).toBe(
      "No audio files found"
    );
  });

  it("falls back to the raw code for an unknown warning", () => {
    expect(getWarningText({ warning: "some_new_code" })).toBe("some_new_code");
  });

  it("reports how many rows were skipped", () => {
    const text = getWarningText({
      warning: "rows_skipped",
      skipped: 8095,
      skip_reasons: { blank_required_field: 8095 },
    });
    expect(text).toContain("8,095 rows skipped");
  });

  it("names the reason rows were skipped", () => {
    const text = getWarningText({
      warning: "rows_skipped",
      skipped: 2,
      skip_reasons: { blank_required_field: 2 },
    });
    expect(text).toContain("blank Artist/Title/Played");
  });

  it("uses the singular for a single skipped row", () => {
    const text = getWarningText({
      warning: "rows_skipped",
      skipped: 1,
      skip_reasons: { extra_fields: 1 },
    });
    expect(text).toContain("1 row skipped");
  });

  it("lists every distinct reason with its count", () => {
    const text = getWarningText({
      warning: "rows_skipped",
      skipped: 3,
      skip_reasons: { blank_required_field: 2, extra_fields: 1 },
    });
    expect(text).toContain("blank Artist/Title/Played: 2");
    expect(text).toContain("too many columns: 1");
  });

  it("falls back to a raw reason code it does not recognise", () => {
    const text = getWarningText({
      warning: "rows_skipped",
      skipped: 1,
      skip_reasons: { future_reason: 1 },
    });
    expect(text).toContain("future_reason");
  });

  it("survives a missing skip_reasons payload", () => {
    const text = getWarningText({ warning: "rows_skipped", skipped: 5 });
    expect(text).toBe("5 rows skipped");
  });

  it("ignores non-numeric reason counts", () => {
    const text = getWarningText({
      warning: "rows_skipped",
      skipped: 1,
      skip_reasons: { blank_required_field: "lots" },
    });
    expect(text).toBe("1 row skipped");
  });
});
