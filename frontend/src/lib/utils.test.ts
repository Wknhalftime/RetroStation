import { describe, it, expect } from "vitest";
import { formatWallClock } from "./utils";

// played_at holds the station's local wall-clock time labelled as UTC, and the
// API may serialize it with the database session's offset. Whatever the offset,
// the displayed time must be the UTC reading of the instant — never the browser's.
describe("formatWallClock", () => {
  it("renders a UTC-labelled wall-clock time as stored", () => {
    expect(formatWallClock("2001-03-15T23:30:00Z")).toBe("Mar 15, 2001, 11:30 PM");
  });

  it("renders the stored wall-clock time when the API adds a session offset", () => {
    expect(formatWallClock("2001-03-15T18:30:00-05:00")).toBe("Mar 15, 2001, 11:30 PM");
  });

  it("keeps a late-evening play on its stored calendar day", () => {
    expect(formatWallClock("2001-03-15T19:59:00-05:00")).toBe("Mar 16, 2001, 12:59 AM");
  });

  it("renders an em dash for a missing value", () => {
    expect(formatWallClock(null)).toBe("—");
  });
});
