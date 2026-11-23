import { describe, expect, it } from "vitest";
import { getWarningText } from "@/lib/taskWarnings";

// Phase 4.1 PR 2b (SPEC S11, D10): a fill run whose calls failed ends with
// `warning: "mb_fill_calls_failed"` and the count in `failed_calls`. The bar shows it in amber
// beside "Done", so a run MusicBrainz half-refused never reads as a clean finish.
describe("getWarningText for a cache fill run with failed calls", () => {
  it("D10: counts the failed calls and says a later run asks them again", () => {
    expect(getWarningText({ warning: "mb_fill_calls_failed", failed_calls: 652 })).toBe(
      "652 calls failed: a later run asks them again"
    );
  });

  it("names one failed call", () => {
    expect(getWarningText({ warning: "mb_fill_calls_failed", failed_calls: 1 })).toBe(
      "1 call failed: a later run asks it again"
    );
  });

  it("groups the thousands", () => {
    expect(getWarningText({ warning: "mb_fill_calls_failed", failed_calls: 1234 })).toBe(
      "1,234 calls failed: a later run asks them again"
    );
  });
});
