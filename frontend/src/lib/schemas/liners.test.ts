// PR L1, Task 6 (traceability LR1, LR4, LR5): the Liners tab's contract with
// GET /api/v1/stations/{station_id}/liners (Task 5): the cadence, and one entry per liner with
// its `cues` state. `cues` is exactly one of edited, auto, failed, analysing or silent.
import { describe, it, expect } from "vitest";
import { LinerLibrarySchema, LinerSchema } from "./liners";

const liner = {
  id: "5d1f6c1e-6a1c-4d6f-9d0e-0d3c3b1f2a10",
  name: "Station ID",
  format: "mp3",
  duration_ms: 8000,
  cues: "auto",
  uploaded_at: "2026-10-17T10:00:00Z",
};

const library = { songs_between_liners: 3, liners: [liner] };

const CUE_STATES = ["edited", "auto", "failed", "analysing", "silent"] as const;

describe("LinerLibrarySchema", () => {
  it("schema accepts the Task 5 list shape and rejects an unknown cues value", () => {
    const parsed = LinerLibrarySchema.parse(library);
    expect(parsed.songs_between_liners).toBe(3);
    expect(parsed.liners).toHaveLength(1);
    expect(parsed.liners[0].id).toBe(liner.id);
    expect(parsed.liners[0].name).toBe("Station ID");
    expect(parsed.liners[0].format).toBe("mp3");
    expect(parsed.liners[0].duration_ms).toBe(8000);
    expect(parsed.liners[0].cues).toBe("auto");

    // LR1: blank = off arrives as null; an empty library is a valid library.
    expect(LinerLibrarySchema.parse({ songs_between_liners: null, liners: [] })).toEqual({
      songs_between_liners: null,
      liners: [],
    });

    // Every cues state the server can send is accepted.
    for (const cues of CUE_STATES) {
      const state = LinerLibrarySchema.parse({ ...library, liners: [{ ...liner, cues }] });
      expect(state.liners[0].cues).toBe(cues);
    }

    // An unknown cues value is rejected, whatever else is in the payload.
    expect(() =>
      LinerLibrarySchema.parse({ ...library, liners: [{ ...liner, cues: "pending" }] })
    ).toThrow();
    expect(() =>
      LinerLibrarySchema.parse({ ...library, liners: [{ ...liner, cues: "Edited" }] })
    ).toThrow();
    expect(() =>
      LinerLibrarySchema.parse({ ...library, liners: [{ ...liner, cues: null }] })
    ).toThrow();
  });

  it("rejects a library whose cadence is not a whole number or null", () => {
    expect(() => LinerLibrarySchema.parse({ ...library, songs_between_liners: 2.5 })).toThrow();
    expect(() => LinerLibrarySchema.parse({ ...library, songs_between_liners: "3" })).toThrow();
    expect(() => LinerLibrarySchema.parse({ liners: [liner] })).toThrow();
  });
});

describe("LinerSchema", () => {
  it("parses one liner and rejects one with no cues state", () => {
    const parsed = LinerSchema.parse(liner);
    expect(parsed.id).toBe(liner.id);
    expect(parsed.name).toBe("Station ID");
    expect(parsed.duration_ms).toBe(8000);
    expect(parsed.cues).toBe("auto");

    const withoutCues: Record<string, unknown> = { ...liner };
    delete withoutCues.cues;
    expect(() => LinerSchema.parse(withoutCues)).toThrow();
    expect(() => LinerSchema.parse({ ...liner, cues: "bogus" })).toThrow();
  });
});
