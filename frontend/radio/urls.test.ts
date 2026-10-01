// Module U: the pages' same-origin paths (plan-f2.md Task 2; traceability-f2.md).
import { describe, expect, it } from "vitest";
import {
  eventsPath,
  parsePlayerPath,
  playerPath,
  streamPath,
} from "../../backend/web/radio/urls.js";

describe("the radio pages' paths", () => {
  // U1 — D71; D72 (the casing is passed through, the server matches any case)
  it.each([
    ["/radio/KIOA/1995", "KIOA", 1995],
    ["/radio/kioa/1995", "kioa", 1995],
    ["/radio/K%20X/2001", "K X", 2001],
  ])("parsePlayerPath reads the call letters and the year [%s]", (path, callLetters, year) => {
    expect(parsePlayerPath(path)).toEqual({ callLetters, year });
  });

  // U2 — error handling (a malformed escape is a `URIError`, caught and named)
  it.each([
    "/radio",
    "/radio/KIOA",
    "/radio/KIOA/19x5",
    "/radio/KIOA/1995/extra",
    "/radio/KIOA/0",
    "/radio/%E0%A4%A/1995",
  ])("a path that is not a station-year gives nothing [%s]", (path) => {
    expect(parsePlayerPath(path)).toBeNull();
  });

  // U3 — contract §2–§3 (relative paths, the same key on both); D74; D28
  it("the stream and events paths are same-origin and carry the key", () => {
    const station = { callLetters: "K X", year: 1995 };
    expect(streamPath(station, "ab/c")).toBe("/listen/K%20X/1995?key=ab%2Fc");
    expect(eventsPath(station, "ab/c")).toBe("/listen/K%20X/1995/events?key=ab%2Fc");
  });

  // U4 — contract §1 (links are built from the stored casing)
  it("the player link is built from the stored call letters", () => {
    expect(playerPath({ callLetters: "K&X", year: 1995 })).toBe("/radio/K%26X/1995");
  });
});
