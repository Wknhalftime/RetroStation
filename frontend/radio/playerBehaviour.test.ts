// Module M: the player's behaviour, black-box through `createPlayer` (plan-f2.md Task 3;
// traceability-f2.md). Assertions are limited to `view()`, what the fakes were told, and times
// on `FakeTimers` (I6). "Stops" and "reconnects at t" are defined in the plan and below.
import { describe, expect, it } from "vitest";
import { playerRig, type PlayerRig } from "./fakes";

const EVENTS = "/listen/KIOA/1995/events?key=k";
const STREAM = "/listen/KIOA/1995?key=k";
const RESTING = ["idle", "ended", "failed"];

function advanceTo(rig: PlayerRig, t: number): void {
  rig.timers.advance(t - rig.timers.now);
}

function status(rig: PlayerRig, kind: string): void {
  rig.latest().emit("status", JSON.stringify({ kind }));
}

function title(rig: PlayerRig, artist: string, song: string): void {
  rig.latest().emit("now_playing", JSON.stringify({ artist, title: song }));
}

const CLOSED = 2; // EventSource.readyState CLOSED

/**
 * Stops: every source is closed (`readyState` 2, however it got there), the audio is paused with
 * its `src` removed, no new source appears in the next 120 s, and the view is resting.
 */
function expectStopped(rig: PlayerRig): void {
  expect(rig.sources.every((source) => source.readyState === CLOSED)).toBe(true);
  expect(rig.audio.paused).toBe(true);
  expect(rig.audio.src).toBe("");
  expect(RESTING).toContain(rig.player.view().phase);
  const count = rig.sources.length;
  rig.timers.advance(120_000);
  expect(rig.sources.length).toBe(count);
  expect(RESTING).toContain(rig.player.view().phase);
}

/** Reconnects at t: a new source at the same events path appears at exactly t, not 1 ms earlier. */
function expectReconnectAt(rig: PlayerRig, t: number): void {
  const count = rig.sources.length;
  advanceTo(rig, t - 1);
  expect(rig.sources.length).toBe(count);
  advanceTo(rig, t);
  expect(rig.sources.length).toBe(count + 1);
  expect(rig.latest().url).toBe(EVENTS);
}

function expectView(rig: PlayerRig, phase: string, reason: string | null = null): void {
  expect(rig.player.view().phase).toBe(phase);
  expect(rig.player.view().reason).toBe(reason);
}

/** A playing player whose audio has run out at t = 0, with no status told. */
function droppedRig(): PlayerRig {
  const rig = playerRig();
  rig.tuneIn();
  rig.audio.fireEnd();
  return rig;
}

describe("the player's behaviour", () => {
  // M1 — contract §3 order; autoplay; D78b (an idle page holds no subscription)
  it("nothing opens before Play, and Play opens the events first", () => {
    const rig = playerRig();
    expect(rig.sources).toHaveLength(0);
    rig.timers.advance(120_000);
    expect(rig.sources).toHaveLength(0);
    rig.player.play();
    expect(rig.sources.map((source) => source.url)).toEqual([EVENTS]);
    expect(rig.audio.srcWrites).toEqual([]);
    expect(rig.audio.playCalls).toBe(0);
    expectView(rig, "connecting");
  });

  // M2 — contract §2–§3 order; D74
  it("the audio gets the stream once the events open, and only once", () => {
    const rig = playerRig();
    rig.connect();
    expect(rig.audio.srcWrites).toEqual([STREAM]);
    expect(rig.audio.playCalls).toBe(1);
    expectView(rig, "tuning");
    rig.latest().fail(false); // the browser's own reconnect of the events stream
    rig.latest().open();
    expect(rig.audio.srcWrites).toEqual([STREAM]);
    expect(rig.audio.playCalls).toBe(1);
    expect(rig.sources).toHaveLength(1);
  });

  // M3 — D78a
  it("the audio playing shows playing", () => {
    const rig = playerRig();
    rig.tuneIn();
    expectView(rig, "playing");
  });

  // M4 — D78a; D42; contract kinds
  it.each([
    ["no_broadcast", "failed", "no_broadcast"],
    ["busy", "failed", "busy"],
    ["unavailable", "failed", "unavailable"],
    ["ended", "ended", null],
  ])("a status while tuning in ends the first try [%s]", (kind, phase, reason) => {
    const rig = playerRig();
    rig.connect();
    status(rig, kind);
    expectView(rig, phase, reason);
    expectStopped(rig);
  });

  // M5 — D78a
  it("stopped while tuning in reconnects", () => {
    const rig = playerRig();
    rig.connect();
    status(rig, "stopped");
    expect(rig.latest().readyState).toBe(CLOSED);
    expectReconnectAt(rig, 1_000);
  });

  // M6 — D78c; contract §3 ("a page whose own audio is still playing treats a status as
  // information only"; "a refusal never hides the playing song")
  it.each(["tuning", "no_broadcast", "busy", "unavailable", "ended", "stopped"])(
    "while its own audio plays, a status is information only [%s]",
    (kind) => {
      const rig = playerRig();
      rig.tuneIn();
      title(rig, "Ace of Base", "The Sign");
      status(rig, kind);
      const song = { artist: "Ace of Base", title: "The Sign" };
      expect(rig.player.view()).toEqual({
        phase: "playing",
        reason: null,
        song,
        titlesLost: false,
      });
      expect(rig.latest().readyState).toBe(1);
      expect(rig.audio.paused).toBe(false);
      rig.timers.advance(120_000);
      expect(rig.sources).toHaveLength(1);
      expect(rig.player.view().phase).toBe("playing");
      expect(rig.player.view().song).toEqual(song);
    }
  );

  // M7 — D13; D73
  it.each([
    [
      "tuning",
      (rig: PlayerRig) => {
        rig.connect();
      },
    ],
    [
      "playing",
      (rig: PlayerRig) => {
        rig.tuneIn();
      },
    ],
    [
      "needsTap",
      async (rig: PlayerRig) => {
        rig.connect();
        await rig.audio.rejectPlay(0, "NotAllowedError");
      },
    ],
  ])("a title is shown while connected [%s]", async (phase, reach) => {
    const rig = playerRig();
    await reach(rig);
    expect(rig.player.view().phase).toBe(phase);
    title(rig, "Hootie & the Blowfish", "Hold My Hand");
    expect(rig.player.view().song).toEqual({
      artist: "Hootie & the Blowfish",
      title: "Hold My Hand",
    });
  });

  // M8 — D78a
  it("its own audio lost after ended was told stops without reconnecting", () => {
    const rig = playerRig();
    rig.tuneIn();
    status(rig, "ended");
    rig.audio.fireEnd();
    expectView(rig, "ended");
    expectStopped(rig);
  });

  // M9 — D78a; I2 (the page waits for its own close, told over the other connection)
  it("ended told within 2 s after its own audio is lost stops without reconnecting", () => {
    const rig = droppedRig();
    rig.timers.advance(1_500);
    status(rig, "ended");
    expectView(rig, "ended");
    expect(rig.sources).toHaveLength(1);
    expectStopped(rig);
  });

  // M10 — D78b
  it.each(["before", "within 2 s"])(
    "unavailable before or just after its own loss does not reconnect [%s]",
    (when) => {
      const rig = playerRig();
      rig.tuneIn();
      if (when === "before") status(rig, "unavailable");
      rig.audio.fireEnd();
      if (when !== "before") {
        rig.timers.advance(1_500);
        status(rig, "unavailable");
      }
      expectView(rig, "failed", "unavailable");
      expect(rig.sources).toHaveLength(1);
      expectStopped(rig);
    }
  );

  // M11 — D78a; D78c; I2
  it.each(["busy", "tuning", "no_broadcast", "none"])(
    "its own loss with another tab's status or none reconnects after 2 s [%s]",
    (kind) => {
      const rig = playerRig();
      rig.tuneIn();
      if (kind !== "none") status(rig, kind);
      rig.audio.fireEnd();
      expectReconnectAt(rig, 3_000);
      expect(RESTING).not.toContain(rig.player.view().phase);
    }
  );

  // M12 — D78a
  it.each([
    ["before", 1_000],
    ["within at +0.5 s", 1_500],
  ])(
    "stopped before or just after its own loss reconnects without the 2 s wait [%s]",
    (when, at) => {
      const rig = playerRig();
      rig.tuneIn();
      if (when === "before") status(rig, "stopped");
      rig.audio.fireEnd();
      if (when !== "before") {
        rig.timers.advance(500);
        status(rig, "stopped");
      }
      expectReconnectAt(rig, at);
    }
  );

  // M13 — D78c (titles resume: the stream went on)
  it("a title after a status means the stream went on", () => {
    const rig = playerRig();
    rig.tuneIn();
    status(rig, "ended");
    title(rig, "Seal", "Kiss from a Rose");
    rig.audio.fireEnd();
    expect(rig.player.view().phase).not.toBe("ended");
    expectReconnectAt(rig, 3_000);
    expect(rig.player.view().phase).not.toBe("ended");
  });

  // M14 — D79b (the waits 1, 2, 4, 8, 16 s; the hard stop 60 s after the drop). The drop is
  // the audio's `ended` while playing (coordinator ruling on D79b's drop moment).
  it("refused tries wait 1, 2, 4, 8 and 16 s, and the player stops 60 s after the drop", () => {
    const rig = droppedRig();
    for (const at of [3_000, 5_000, 9_000, 17_000, 33_000]) {
      expectReconnectAt(rig, at);
      rig.latest().fail(true);
    }
    advanceTo(rig, 59_999);
    expect(rig.sources).toHaveLength(6);
    expectView(rig, "retrying");
    advanceTo(rig, 60_000);
    expectView(rig, "failed", "lost");
    expectStopped(rig);
  });

  // M15 — D79b; review I4 (tries that never open each give up after 10 s). Here 60 s falls
  // between tries; M36 lands the cut-off during a try.
  it("when tries hang, each gives up after 10 s, and the player stops at 60 s", () => {
    const rig = droppedRig();
    for (const at of [3_000, 15_000, 29_000, 47_000]) expectReconnectAt(rig, at);
    advanceTo(rig, 59_999);
    expect(rig.sources).toHaveLength(5);
    expect(RESTING).not.toContain(rig.player.view().phase);
    advanceTo(rig, 60_000);
    expectView(rig, "failed", "lost");
    expect(rig.sources[4]?.readyState).toBe(CLOSED);
    expectStopped(rig);
  });

  // M16 — D79b. The drop is the start of a stall that lasts 10 s (coordinator ruling on D79b's
  // drop moment): the cut-off is 60 s from the first `waiting`, not from the stall's verdict.
  it("a drop found by a 10 s stall stops 60 s after the stall began", () => {
    const rig = playerRig();
    rig.tuneIn();
    rig.audio.fireWaiting();
    for (const at of [11_000, 13_000, 17_000, 25_000, 41_000]) {
      expectReconnectAt(rig, at);
      rig.latest().fail(true);
    }
    advanceTo(rig, 59_999);
    expectView(rig, "retrying");
    advanceTo(rig, 60_000);
    expectView(rig, "failed", "lost");
    expectStopped(rig);
  });

  // M35 (added in the draft; not in the plan's list) — D79b. The coordinator's ruling on D79b's
  // drop moment names three: the audio's `ended` or `error` while playing (M14 pins `ended`),
  // the start of a 10 s stall (M16), and a `stopped` status while tuning in. This pins the
  // other two, so every drop moment the ruling names is measured.
  it.each([
    ["an audio error while playing", [3_000, 5_000, 9_000, 17_000, 33_000]],
    ["stopped while tuning in", [1_000, 3_000, 7_000, 15_000, 31_000]],
  ])("the 60 s cut-off also runs from %s", (drop, tries) => {
    const rig = playerRig();
    if (drop === "stopped while tuning in") {
      rig.connect();
      status(rig, "stopped");
    } else {
      rig.tuneIn();
      rig.audio.fireError();
    }
    for (const at of tries) {
      expectReconnectAt(rig, at);
      rig.latest().fail(true);
    }
    advanceTo(rig, 59_999);
    expectView(rig, "retrying");
    advanceTo(rig, 60_000);
    expectView(rig, "failed", "lost");
    expectStopped(rig);
  });

  // M36 — D79b ("whatever try it is on"; audit MF1): the cut-off lands while a try is still
  // connecting, or tuning in, at 60 s, and stops it there.
  it.each(["connecting", "tuning"])("the cut-off stops a try still %s at 60 s", (phase) => {
    const rig = droppedRig();
    if (phase === "connecting") {
      expectReconnectAt(rig, 3_000); // hangs: gives up at 13 s
      expectReconnectAt(rig, 15_000); // hangs: gives up at 25 s
      expectReconnectAt(rig, 29_000);
      rig.latest().fail(true);
      expectReconnectAt(rig, 37_000);
      rig.latest().fail(true);
      expectReconnectAt(rig, 53_000); // still connecting at 60 s
    } else {
      for (const at of [3_000, 15_000, 29_000, 47_000]) expectReconnectAt(rig, at);
      rig.latest().open(); // tuning in; its own 15 s limit would end at 62 s
    }
    advanceTo(rig, 59_999);
    expectView(rig, phase);
    advanceTo(rig, 60_000);
    expectView(rig, "failed", "lost");
    expectStopped(rig);
  });

  // M37 — I3; D79b (audit SF2): on a reconnect, an audio failure, or a tune-in that never plays,
  // is retried within the minute, not final as on a first tune-in (plan rows 9 and 10).
  it.each(["an audio error", "no playing in 15 s"])(
    "a reconnect whose audio fails is retried within the minute [%s]",
    (how) => {
      const rig = droppedRig();
      expectReconnectAt(rig, 3_000);
      rig.latest().open();
      if (how === "an audio error") rig.audio.fireError();
      advanceTo(rig, 30_000);
      expect(RESTING).not.toContain(rig.player.view().phase);
      expect(rig.sources.length).toBeGreaterThan(2);
      expect(rig.latest().url).toBe(EVENTS);
    }
  );

  // M17 — D79b; D11 (a recovery cancels the cut-off; a later drop gets a fresh 60 s and the
  // waits start again)
  it("a reconnect that plays again cancels the cut-off", () => {
    const rig = droppedRig();
    expectReconnectAt(rig, 3_000);
    rig.latest().open();
    rig.audio.firePlaying();
    advanceTo(rig, 120_000);
    expectView(rig, "playing");
    rig.audio.fireEnd();
    expectReconnectAt(rig, 123_000); // the waits start again from 1 s (audit SF1)
    advanceTo(rig, 179_999);
    expect(RESTING).not.toContain(rig.player.view().phase);
    advanceTo(rig, 180_000);
    expectView(rig, "failed", "lost");
    expectStopped(rig);
  });

  // M18 — D14 (the page cannot read HTTP codes); contract §3 (no tight loop)
  it.each(["closed", "10 s with no open"])(
    "a refused or silent first connection says could not tune in and never retries [%s]",
    (how) => {
      const rig = playerRig();
      rig.player.play();
      if (how === "closed") {
        rig.latest().fail(true);
      } else {
        rig.timers.advance(9_999);
        expectView(rig, "connecting");
        rig.timers.advance(1);
      }
      expectView(rig, "failed", "unreachable");
      expect(rig.sources).toHaveLength(1);
      expectStopped(rig);
    }
  );

  // M19 — contract §3 (the backlog cut, and blips, are left to EventSource's own reconnect)
  it.each([
    ["connecting", (rig: PlayerRig) => rig.player.play()],
    ["tuning", (rig: PlayerRig) => rig.connect()],
    ["playing", (rig: PlayerRig) => rig.tuneIn()],
  ])("an events blip is left to the browser [%s]", (phase, reach) => {
    const rig = playerRig();
    reach(rig);
    expect(rig.player.view().phase).toBe(phase);
    const before = rig.player.view();
    const writes = rig.audio.srcWrites.length;
    const paused = rig.audio.paused;
    rig.latest().fail(false);
    expect(rig.player.view()).toEqual(before);
    expect(rig.latest().readyState).not.toBe(CLOSED);
    expect(rig.sources).toHaveLength(1);
    expect(rig.audio.srcWrites).toHaveLength(writes);
    expect(rig.audio.removals).toBe(0);
    expect(rig.audio.paused).toBe(paused);
  });

  // M20 — I3 (on a reconnect the busy slot may be the page's own dropped stream); D42; D79b
  it("busy during a reconnect is tried again within the minute", () => {
    const rig = droppedRig();
    expectReconnectAt(rig, 3_000);
    rig.latest().open();
    expectView(rig, "tuning");
    status(rig, "busy");
    expectView(rig, "retrying");
    expectReconnectAt(rig, 5_000);
  });

  // M21 — D78a; D78b; D43
  it.each([
    ["no_broadcast", "failed", "no_broadcast"],
    ["unavailable", "failed", "unavailable"],
    ["ended", "ended", null],
  ])("no_broadcast, unavailable or ended during a reconnect is final [%s]", (kind, phase, why) => {
    const rig = droppedRig();
    expectReconnectAt(rig, 3_000);
    rig.latest().open();
    status(rig, kind);
    expectView(rig, phase, why);
    expect(rig.sources).toHaveLength(2);
    expectStopped(rig);
  });

  // M22 — D14; D78a
  it("an audio failure while tuning in waits 2 s for a reason, then gives up", () => {
    const rig = playerRig();
    rig.connect();
    rig.audio.fireError();
    rig.timers.advance(1_999);
    expectView(rig, "tuning");
    rig.timers.advance(1);
    expectView(rig, "failed", "unreachable");
    expectStopped(rig);
  });

  // M23 — D78a; D42
  it.each(["busy", "no_broadcast"])("a reason in that wait wins [%s]", (kind) => {
    const rig = playerRig();
    rig.connect();
    rig.audio.fireError();
    rig.timers.advance(500);
    status(rig, kind);
    expectView(rig, "failed", kind);
    expectStopped(rig);
  });

  // M24 — review minor 2 (the 15 s tune-in timeout)
  it("tuning in that never plays gives up after 15 s", () => {
    const rig = playerRig();
    rig.connect();
    rig.timers.advance(14_999);
    expectView(rig, "tuning");
    rig.timers.advance(1);
    expectView(rig, "failed", "unreachable");
    expectStopped(rig);
  });

  // M25 — contract notes (connections); §3 (leaving frees the subscription)
  it.each([
    ["connecting", async (rig: PlayerRig) => rig.player.play()],
    ["tuning", async (rig: PlayerRig) => rig.connect()],
    ["playing", async (rig: PlayerRig) => rig.tuneIn()],
    [
      "retrying",
      async (rig: PlayerRig) => {
        rig.tuneIn();
        rig.audio.fireEnd();
        rig.timers.advance(2_000);
      },
    ],
    [
      "needsTap",
      async (rig: PlayerRig) => {
        rig.connect();
        await rig.audio.rejectPlay(0, "NotAllowedError");
      },
    ],
  ])("Stop closes everything from every active phase [%s]", async (phase, reach) => {
    const rig = playerRig();
    await reach(rig);
    expect(rig.player.view().phase).toBe(phase);
    rig.player.stop();
    expect(rig.player.view()).toEqual({
      phase: "idle",
      reason: null,
      song: null,
      titlesLost: false,
    });
    expectStopped(rig);
  });

  // M26 — design question 3 (an interruption: a call, unplugged headphones); D11
  it("a pause the page did not ask for stops like Stop", () => {
    const rig = playerRig();
    rig.tuneIn();
    rig.audio.pause(); // from outside the page; the browser queues the event
    rig.timers.advance(0);
    expectView(rig, "idle");
    expectStopped(rig);
  });

  // M27 — design question 3 (a live MP3 that hangs in `waiting`)
  it("a 10 s stall while playing counts as a drop", () => {
    const rig = playerRig();
    rig.tuneIn();
    rig.audio.fireWaiting();
    expectReconnectAt(rig, 11_000);
  });

  // M28 — design question 3
  it("playback resuming within 10 s cancels the stall", () => {
    const rig = playerRig();
    rig.tuneIn();
    rig.audio.fireWaiting();
    rig.timers.advance(9_000);
    rig.audio.firePlaying();
    advanceTo(rig, 120_000);
    expect(rig.sources).toHaveLength(1);
    expectView(rig, "playing");
  });

  // M29 — contract notes (autoplay: start the audio from the Play button)
  it("a blocked autoplay asks for a tap, and the tap plays without reopening the events", async () => {
    const rig = playerRig();
    rig.connect();
    await rig.audio.rejectPlay(0, "NotAllowedError");
    expectView(rig, "needsTap");
    expect(rig.latest().readyState).toBe(1);
    rig.player.play();
    expect(rig.audio.playCalls).toBe(2);
    expect(rig.sources).toHaveLength(1);
    expect(rig.latest().readyState).toBe(1);
    expectView(rig, "tuning");
  });

  // M30 — review minor 2 (a page left waiting for a tap)
  it("a page left waiting for a tap goes back to idle after 30 s", async () => {
    const rig = playerRig();
    rig.connect();
    await rig.audio.rejectPlay(0, "NotAllowedError");
    rig.timers.advance(29_999);
    expectView(rig, "needsTap");
    rig.timers.advance(1);
    expectView(rig, "idle");
    expectStopped(rig);
  });

  // M31 — D73 (no stale title)
  it("a lost title feed keeps the music playing", () => {
    const rig = playerRig();
    rig.tuneIn();
    title(rig, "TLC", "Waterfalls");
    rig.latest().fail(true);
    expect(rig.player.view()).toEqual({
      phase: "playing",
      reason: null,
      song: null,
      titlesLost: true,
    });
    expect(rig.latest().readyState).toBe(CLOSED);
    expect(rig.audio.paused).toBe(false);
    expect(rig.audio.srcWrites).toEqual([STREAM]);
    expect(rig.audio.removals).toBe(0);
  });

  // M32 — design question 3 (a hidden tab's timers may be throttled)
  it("a page shown again while waiting to reconnect reconnects at once", () => {
    const rig = droppedRig();
    rig.timers.advance(2_000);
    expectView(rig, "retrying");
    expect(rig.sources).toHaveLength(1);
    rig.player.visible();
    expect(rig.sources).toHaveLength(2);
    expect(rig.latest().url).toBe(EVENTS);
    rig.timers.advance(1_000);
    expect(rig.sources).toHaveLength(2);
  });

  // M38 — design question 3 (audit SF3): becoming visible while playing changes nothing.
  it("a playing page shown again keeps playing", () => {
    const rig = playerRig();
    rig.tuneIn();
    rig.player.visible();
    rig.timers.advance(0);
    expect(rig.sources).toHaveLength(1);
    expect(rig.latest().readyState).toBe(1);
    expect(rig.audio.paused).toBe(false);
    expect(rig.audio.srcWrites).toEqual([STREAM]);
    expectView(rig, "playing");
  });

  // M39 — D78a; D42; D43 (audit N7, plan rows 5 and 6 in `needsTap`): a final status while the
  // page waits for a tap ends the wait at once, as it would while tuning in.
  it.each([
    ["ended", "ended", null],
    ["no_broadcast", "failed", "no_broadcast"],
    ["unavailable", "failed", "unavailable"],
    ["busy", "failed", "busy"],
  ])("a final status while waiting for a tap ends the wait [%s]", async (kind, phase, why) => {
    const rig = playerRig();
    rig.connect();
    await rig.audio.rejectPlay(0, "NotAllowedError");
    expectView(rig, "needsTap");
    status(rig, kind);
    expectView(rig, phase, why);
    expectStopped(rig);
  });

  // M33 — D78a
  it.each([
    ["ended", "ended"],
    ["failed(busy)", "busy"],
  ])("Play after an end or a failure starts afresh [%s]", (_label, kind) => {
    const rig = playerRig();
    rig.connect();
    status(rig, kind);
    expect(RESTING).toContain(rig.player.view().phase);
    rig.player.play();
    expect(rig.sources).toHaveLength(2);
    expect(rig.latest().url).toBe(EVENTS);
    expect(rig.latest().readyState).not.toBe(CLOSED);
    expect(rig.player.view()).toEqual({
      phase: "connecting",
      reason: null,
      song: null,
      titlesLost: false,
    });
  });

  // M34 — autoplay (nothing starts without Play); no stale effects
  it.each([
    ["fireEnd", (rig: PlayerRig) => rig.audio.fireEnd()],
    ["firePlaying", (rig: PlayerRig) => rig.audio.firePlaying()],
    ["visible()", (rig: PlayerRig) => rig.player.visible()],
    ["120 s pass", (rig: PlayerRig) => rig.timers.advance(120_000)],
  ])("a resting player ignores everything but Play [%s]", (_label, poke) => {
    const rig = playerRig();
    const before = rig.player.view();
    poke(rig);
    rig.timers.advance(120_000);
    expect(rig.sources).toHaveLength(0);
    expect(rig.player.view()).toEqual(before);
    expect(before.phase).toBe("idle");
  });
});
