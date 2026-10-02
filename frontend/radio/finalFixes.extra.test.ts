// Unlocked tests for the PR F2 final-review fixes (final-review.md M1, M5, M6). The locked
// acceptance tests stay as they are; these only pin the behaviour the fixes add.
import { describe, expect, it } from "vitest";
import { loadStationYears } from "../../backend/web/radio/stationList.js";
import { playerRig, type PlayerRig } from "./fakes";

const CLOSED = 2; // EventSource.readyState CLOSED

function status(rig: PlayerRig, kind: string): void {
  rig.latest().emit("status", JSON.stringify({ kind }));
}

function title(rig: PlayerRig, artist: string, song: string): void {
  rig.latest().emit("now_playing", JSON.stringify({ artist, title: song }));
}

describe("the final-review fixes", () => {
  // M1 — D73: a title replayed from an older stream goes once this page's stream is admitted.
  it.each([
    ["tuning", async (rig: PlayerRig) => rig.connect()],
    [
      "needsTap",
      async (rig: PlayerRig) => {
        rig.connect();
        await rig.audio.rejectPlay(0, "NotAllowedError");
      },
    ],
  ])("a tuning status clears an older stream's title [%s]", async (phase, reach) => {
    const rig = playerRig();
    await reach(rig);
    title(rig, "Ace of Base", "The Sign");
    status(rig, "tuning");
    expect(rig.player.view().phase).toBe(phase);
    expect(rig.player.view().song).toBeNull();
    title(rig, "Seal", "Kiss from a Rose");
    expect(rig.player.view().song).toEqual({ artist: "Seal", title: "Kiss from a Rose" });
  });

  // M5 — D79b: a reconnect whose play() is refused and never tapped has lost the station.
  it("a reconnect left waiting for a tap ends in failed(lost)", async () => {
    const rig = playerRig();
    rig.tuneIn();
    rig.audio.fireEnd(); // the drop at t = 0; the next try opens at 3 s
    rig.timers.advance(3_000);
    expect(rig.sources).toHaveLength(2);
    rig.latest().open();
    await rig.audio.rejectPlay(1, "NotAllowedError");
    expect(rig.player.view().phase).toBe("needsTap");
    rig.timers.advance(29_999);
    expect(rig.player.view().phase).toBe("needsTap");
    rig.timers.advance(1);
    expect(rig.player.view()).toEqual({
      phase: "failed",
      reason: "lost",
      song: null,
      titlesLost: false,
    });
    expect(rig.sources.every((source) => source.readyState === CLOSED)).toBe(true);
    expect(rig.audio.paused).toBe(true);
    expect(rig.audio.src).toBe("");
  });

  // M6 — a body cut off while being read is told as a failed load, never thrown.
  it("a body that cannot be read gives a failed load", async () => {
    const rows = await loadStationYears(async () => ({
      ok: true,
      json: async () => Promise.reject(new TypeError("network error")),
    }));
    expect(rows).toBeNull();
  });
});
