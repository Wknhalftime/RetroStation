import { describe, expect, it } from "vitest";
import { getWarningText } from "@/lib/taskWarnings";

// Spec 2026-10-17 "Matching run progress": a matching run whose playlist stages partly failed
// completes with `warning: "matching_stages_failed"` and the count in `stages_failed`.
describe("getWarningText for a matching run with failed stages", () => {
  it("names one failed playlist stage", () => {
    expect(getWarningText({ warning: "matching_stages_failed", stages_failed: 1 })).toBe(
      "1 playlist stage failed — see System Logs"
    );
  });

  it("names several failed playlist stages", () => {
    expect(getWarningText({ warning: "matching_stages_failed", stages_failed: 3 })).toBe(
      "3 playlist stages failed — see System Logs"
    );
  });
});
