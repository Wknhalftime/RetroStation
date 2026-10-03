// PR G1, Task 5 (traceability VA1, VA2; rows S1, S5, S7, S21, PG1, PG3, M2): the Streaming page's
// contract with GET /api/v1/streaming/settings (plan, Global Constraints: five keys; the sign-off
// as name, seconds and format; D10, D27, D34, PG1, PG3, M2).
import { describe, it, expect } from "vitest";
import { StreamSettingsSchema } from "./streaming";

const settings = {
  streaming: "on",
  max_sessions: 3,
  max_sessions_problem: null,
  sign_off: { name: "Good night.mp3", seconds: 12.5, format: "mp3" },
  sign_off_problem: null,
};

describe("StreamSettingsSchema", () => {
  it("VA1: parses the settings, with a sign-off and its format", () => {
    const parsed = StreamSettingsSchema.parse(settings);
    expect(parsed.sign_off).toEqual({ name: "Good night.mp3", seconds: 12.5, format: "mp3" });
    expect(parsed.max_sessions).toBe(3);
    expect(StreamSettingsSchema.parse({ ...settings, sign_off: null }).sign_off).toBeNull();
  });

  it("VA2: reads the problems; rejects a bad state, a limit below 1, an unknown format", () => {
    const problems = StreamSettingsSchema.parse({
      ...settings,
      streaming: "unavailable",
      max_sessions: null,
      max_sessions_problem: "user_settings.stream_max_sessions must be a whole number >= 1",
      sign_off_problem: "the clip's file is missing; upload it again",
    });
    expect(problems.max_sessions_problem).toContain("stream_max_sessions");
    expect(problems.sign_off_problem).toBe("the clip's file is missing; upload it again");

    expect(() => StreamSettingsSchema.parse({ ...settings, streaming: "paused" })).toThrow();
    expect(() => StreamSettingsSchema.parse({ ...settings, max_sessions: 0 })).toThrow();
    expect(() => StreamSettingsSchema.parse({ ...settings, max_sessions: 2.5 })).toThrow();
    expect(() =>
      StreamSettingsSchema.parse({
        ...settings,
        sign_off: { name: "a.ogg", seconds: 3, format: "ogg" },
      })
    ).toThrow();
  });
});
