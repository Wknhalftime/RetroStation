import { describe, it, expect } from "vitest";
import { deletionPrompt } from "./deletionPrompt";

describe("deletionPrompt", () => {
  it("names the rows and the matches they release", () => {
    expect(deletionPrompt(2, 3)).toBe(
      "Delete 2 missing files from the library? 3 matches are released: " +
        "those broadcast songs go back to review."
    );
  });

  it("uses the singular", () => {
    expect(deletionPrompt(1, 1)).toBe(
      "Delete 1 missing file from the library? 1 match is released: " +
        "that broadcast song goes back to review."
    );
  });

  it("says when nothing is released", () => {
    expect(deletionPrompt(4, 0)).toBe(
      "Delete 4 missing files from the library? No matches are released."
    );
  });
});
