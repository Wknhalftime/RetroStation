// Module C: the player's browser bindings — the SSE frames, the `<audio>` element and the lock
// screen (MediaSession) — black-box through `playerRig()` (plan-f2.md Task 3;
// traceability-f2.md). Assertions are limited to `view()` and what the fakes were told (I6).
import { describe, expect, it } from "vitest";
import { playerRig, type PlayerRig } from "./fakes";

const EVENTS = "/listen/KIOA/1995/events?key=k";
const STREAM = "/listen/KIOA/1995?key=k";
const CLOSED = 2; // EventSource.readyState CLOSED

/** Lets the player's promise reactions run (microtasks only; no timer). */
async function flush(): Promise<void> {
  for (let i = 0; i < 10; i++) await Promise.resolve();
}

function title(rig: PlayerRig, artist: string, song: string): void {
  rig.latest().emit("now_playing", JSON.stringify({ artist, title: song }));
}

function session(rig: PlayerRig) {
  const media = rig.media;
  if (media === null) throw new Error("this rig has no MediaSession");
  return media;
}

describe("the player's browser bindings", () => {
  // C1 — D13; D78a; contract §3 frames
  it("status and now_playing frames reach the page", () => {
    const rig = playerRig();
    rig.connect();
    rig.latest().emit("now_playing", '{"artist": "Natalie Merchant", "title": "Carnival"}');
    expect(rig.player.view().song).toEqual({ artist: "Natalie Merchant", title: "Carnival" });
    rig.latest().emit("status", '{"kind":"busy"}');
    expect(rig.player.view().phase).toBe("failed");
    expect(rig.player.view().reason).toBe("busy");
  });

  // C2 — CLAUDE.md (narrow `unknown`; never skip error handling)
  it.each([
    ["bad JSON", "status", '{"kind": "busy"'],
    ["no title", "now_playing", '{"artist": "Bush"}'],
    ["a number as the artist", "now_playing", '{"artist": 7, "title": "Glycerine"}'],
    ["an unknown kind", "status", '{"kind": "on_fire"}'],
  ])("a malformed frame is ignored [%s]", (_label, type, data) => {
    const rig = playerRig();
    rig.connect();
    title(rig, "Bush", "Comedown");
    const before = rig.player.view();
    expect(() => rig.latest().emit(type, data)).not.toThrow();
    expect(rig.player.view()).toEqual(before);
    expect(rig.latest().readyState).toBe(1);
    expect(rig.sources).toHaveLength(1);
  });

  // C3 — contract §3; defensive (audit SF7). A browser never delivers a frame from a closed
  // EventSource; the fake deliberately does, so this proves the player's own guard: frames from a
  // source that is no longer current are ignored.
  it("frames from a closed events stream are ignored", () => {
    const stopped = playerRig();
    stopped.tuneIn();
    const first = stopped.latest();
    stopped.player.stop();
    const idle = stopped.player.view();
    first.emit("now_playing", '{"artist": "Live", "title": "Lightning Crashes"}');
    first.emit("status", '{"kind": "busy"}');
    expect(stopped.player.view()).toEqual(idle);

    const retried = playerRig();
    retried.connect();
    const old = retried.latest();
    old.emit("status", '{"kind": "stopped"}');
    const retrying = retried.player.view();
    expect(retrying.phase).toBe("retrying");
    old.emit("now_playing", '{"artist": "Live", "title": "Lightning Crashes"}');
    old.emit("status", '{"kind": "busy"}');
    expect(retried.player.view()).toEqual(retrying);
    retried.timers.advance(1_000);
    retried.latest().open();
    const tuning = retried.player.view();
    old.emit("status", '{"kind": "ended"}');
    old.emit("now_playing", '{"artist": "Live", "title": "Lightning Crashes"}');
    expect(retried.player.view()).toEqual(tuning);
  });

  // C4 — contract notes (connections). An implementation pin, accepted as the browser's idiom
  // for releasing a stream (review minor 11): never `src = ""`.
  it("Stop silences the audio with the browser's idiom", () => {
    const rig = playerRig();
    rig.tuneIn();
    rig.player.stop();
    expect(rig.audio.paused).toBe(true);
    expect(rig.audio.removals).toBe(1);
    expect(rig.audio.loads).toBe(1);
    expect(rig.audio.srcWrites).not.toContain("");
  });

  // C5 — D78a (the `pause` a browser fires just before `ended` is not a user pause)
  it("the pause that ends a stream is not a listener's pause", () => {
    const rig = playerRig();
    rig.tuneIn();
    rig.audio.fireEnd();
    rig.timers.advance(0);
    expect(rig.player.view().phase).not.toBe("idle");
    rig.timers.advance(3_000);
    expect(rig.sources).toHaveLength(2);
    expect(rig.player.view().phase).not.toBe("idle");
  });

  // C6 — contract notes (autoplay)
  it.each([
    ["NotAllowedError", "needsTap"],
    ["AbortError", "tuning"],
  ])("a play() refusal [%s]", async (name, phase) => {
    const rig = playerRig();
    rig.connect();
    await rig.audio.rejectPlay(0, name);
    expect(rig.player.view().phase).toBe(phase);
    expect(rig.latest().readyState).toBe(1);
  });

  // C7 — review minor 3; defensive (audit N4). A browser settles a refused play() within a
  // microtask, so a stale NotAllowedError cannot reach a later try. With the browser-true fake
  // (audit N6) the earlier try's play() ends in AbortError when the retry stops the audio; that
  // settlement must not disturb the later try, whose own refusal still asks for a tap.
  it("a play() refusal from an earlier try is ignored", async () => {
    const rig = playerRig();
    rig.connect();
    rig.latest().emit("status", '{"kind": "stopped"}');
    await flush();
    rig.timers.advance(1_000);
    rig.latest().open();
    await flush();
    expect(rig.audio.playCalls).toBe(2);
    expect(rig.player.view().phase).toBe("tuning");
    await rig.audio.rejectPlay(1, "NotAllowedError");
    expect(rig.player.view().phase).toBe("needsTap");
  });

  // C8 — D74; D11 (the bookmark is the key: Stop/Play and every reconnect resume from it)
  it("Stop then Play, and every reconnect, keep the same key", () => {
    const rig = playerRig();
    rig.tuneIn();
    rig.player.stop();
    rig.player.play();
    rig.latest().open();
    rig.audio.firePlaying();
    rig.audio.fireEnd();
    rig.timers.advance(3_000);
    rig.latest().open();
    expect(rig.sources.map((source) => source.url)).toEqual([EVENTS, EVENTS, EVENTS]);
    expect(rig.audio.srcWrites).toEqual([STREAM, STREAM, STREAM]);
  });

  // C9 — design question 3 (the lock screen)
  it.each([
    ["play", "connecting"],
    ["pause", "idle"],
    ["stop", "idle"],
  ])("the lock screen's controls drive the player [%s]", (action, phase) => {
    const rig = playerRig();
    if (action !== "play") rig.tuneIn();
    session(rig).press(action);
    expect(rig.player.view().phase).toBe(phase);
    if (action !== "play") expect(rig.latest().readyState).toBe(CLOSED);
    else expect(rig.sources).toHaveLength(1);
  });

  // C10 — D73 (the artist and the title only)
  it("the lock screen shows the artist and the title only", () => {
    const rig = playerRig();
    rig.tuneIn();
    title(rig, "Gin Blossoms", "Hey Jealousy");
    expect(session(rig).metadata).toEqual({ title: "Hey Jealousy", artist: "Gin Blossoms" });
    title(rig, "Counting Crows", "Mr. Jones");
    expect(session(rig).metadata).toEqual({ title: "Mr. Jones", artist: "Counting Crows" });
    expect(session(rig).inits.length).toBeGreaterThanOrEqual(2);
    for (const init of session(rig).inits)
      expect(Object.keys(init).sort()).toEqual(["artist", "title"]);
  });

  // C11 — D73 (no stale title on the lock screen); review minor 8
  it.each([
    [
      "reconnecting",
      (rig: PlayerRig) => {
        rig.audio.fireEnd();
        rig.timers.advance(2_000);
      },
    ],
    ["titles lost", (rig: PlayerRig) => rig.latest().fail(true)],
    ["stopped", (rig: PlayerRig) => rig.player.stop()],
  ])("the lock screen drops the song whenever the page does [%s]", (_label, clear) => {
    const rig = playerRig();
    rig.tuneIn();
    title(rig, "Sheryl Crow", "All I Wanna Do");
    expect(session(rig).metadata).not.toBeNull();
    clear(rig);
    expect(rig.player.view().song).toBeNull();
    expect(session(rig).metadata).toBeNull();
  });

  // C12 — design question 3. While tuning in, the lock screen may show "playing" or "paused"
  // (a plan choice the spec does not make; audit N5): only that the session is active is pinned.
  it.each([
    ["playing", ["playing"], (rig: PlayerRig) => rig.tuneIn()],
    ["tuning", ["playing", "paused"], (rig: PlayerRig) => rig.connect()],
    [
      "idle",
      ["none"],
      (rig: PlayerRig) => {
        rig.tuneIn();
        rig.player.stop();
      },
    ],
  ])("the lock screen's play state follows the player [%s]", (phase, states, reach) => {
    const rig = playerRig();
    reach(rig);
    expect(rig.player.view().phase).toBe(phase);
    expect(states).toContain(session(rig).playbackState);
  });

  // C13 — feature detection (no MediaSession on some plain-http pages, R2)
  it("without MediaSession the player still plays", () => {
    const rig = playerRig({ media: false });
    expect(rig.media).toBeNull();
    rig.tuneIn();
    title(rig, "Toad the Wet Sprocket", "Walk on the Ocean");
    expect(rig.player.view().phase).toBe("playing");
    expect(rig.player.view().song).toEqual({
      artist: "Toad the Wet Sprocket",
      title: "Walk on the Ocean",
    });
  });

  // C14 — review minor 4 (some browsers throw a TypeError for an unsupported action)
  it("a browser that refuses the stop action still gets a player", () => {
    const rig = playerRig({ unsupported: ["stop"] });
    session(rig).press("play");
    expect(rig.player.view().phase).toBe("connecting");
    rig.latest().open();
    rig.audio.firePlaying();
    expect(rig.player.view().phase).toBe("playing");
    session(rig).press("pause");
    expect(rig.player.view().phase).toBe("idle");
    expect(rig.latest().readyState).toBe(CLOSED);
  });
});
