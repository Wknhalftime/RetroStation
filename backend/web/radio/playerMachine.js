// The player's state machine (plan-f2.md design question 2, "internal design"; private to
// player.js). `step` is pure: it returns the next state and, in order, the effects the
// controller performs on the browser objects. It holds no timer, socket or element itself.

/** @typedef {import("./player.js").Phase} Phase */
/** @typedef {import("./player.js").FailReason} FailReason */
/** @typedef {import("./player.js").Song} Song */
/** @typedef {import("./player.js").StatusKind} StatusKind */

/**
 * The player's state. Beyond the view's four fields:
 * - `lastStatus`: the latest status told while the page's own audio plays (D78c); a title
 *   clears it, because the stream went on;
 * - `attempt`: the tries since the drop, which set the wait before the next one (D79b);
 * - `reconnecting`: a drop has happened and its 60 s deadline runs;
 * - `lossPending`: the audio was lost, and the page waits up to 2 s for a reason (I2);
 * - `stalling`: the 10 s stall watchdog runs.
 *
 * @typedef {{
 *   phase: Phase,
 *   reason: FailReason | null,
 *   song: Song | null,
 *   titlesLost: boolean,
 *   lastStatus: StatusKind | null,
 *   attempt: number,
 *   reconnecting: boolean,
 *   lossPending: boolean,
 *   stalling: boolean
 * }} PlayerState
 */

/** @typedef {"open" | "tune" | "grace" | "stall" | "tap" | "deadline" | "wait"} TimerName */

/**
 * What happens to the player: the listener's commands, the events stream, the audio element,
 * and a timer running out.
 *
 * @typedef {{type: "play"} | {type: "stop"} | {type: "visible"} | {type: "eventsOpen"}
 *   | {type: "eventsFailed", closed: boolean} | {type: "status", kind: StatusKind}
 *   | {type: "nowPlaying", song: Song} | {type: "audioPlaying"} | {type: "audioLost"}
 *   | {type: "audioWaiting"} | {type: "audioPaused"} | {type: "playBlocked"}
 *   | {type: "timeout", timer: TimerName}} PlayerEvent
 */

/**
 * What the controller does for a transition. `startAudio` gives the audio the stream and
 * plays it; `resumeAudio` only plays it (inside the listener's tap); `stopAudio` silences it
 * and frees the stream. Setting a timer that runs replaces it.
 *
 * @typedef {{type: "openEvents"} | {type: "closeEvents"} | {type: "startAudio"}
 *   | {type: "resumeAudio"} | {type: "stopAudio"}
 *   | {type: "setTimer", timer: TimerName, ms: number}
 *   | {type: "clearTimer", timer: TimerName}} Effect
 */

/** @typedef {{state: PlayerState, effects: Effect[]}} Transition */

const OPEN_MS = 10_000; // the events stream opening
const TUNE_MS = 15_000; // from the audio's source to `playing` (review minor 2)
const GRACE_MS = 2_000; // waiting for a reason once the audio is lost (I2)
const STALL_MS = 10_000; // a `waiting` that lasts this long is a drop
const TAP_MS = 30_000; // waiting for a tap (review minor 2)
const DEADLINE_MS = 60_000; // the hard cut-off from the drop (D79b)
const FIRST_WAIT_MS = 1_000;
const LONGEST_WAIT_MS = 30_000;

/** @type {readonly TimerName[]} */
const TIMERS = ["open", "tune", "grace", "stall", "tap", "deadline", "wait"];

/**
 * @returns {PlayerState}
 */
export function initialState() {
  return {
    phase: "idle",
    reason: null,
    song: null,
    titlesLost: false,
    lastStatus: null,
    attempt: 0,
    reconnecting: false,
    lossPending: false,
    stalling: false,
  };
}

/**
 * `retryDelay(n) = min(1000 * 2^(n-1), 30000)` ms (D79b): 1, 2, 4, 8, 16, then 30 s.
 *
 * @param {number} attempt
 * @returns {number}
 */
export function retryDelay(attempt) {
  return Math.min(FIRST_WAIT_MS * 2 ** (attempt - 1), LONGEST_WAIT_MS);
}

/**
 * Whether `phase` is a resting one (`idle`, `ended` or `failed`): nothing open, Play shown.
 *
 * @param {Phase} phase
 * @returns {phase is "idle" | "ended" | "failed"}
 */
export function isResting(phase) {
  return phase === "idle" || phase === "ended" || phase === "failed";
}

/**
 * A transition with no effects: `state` as given, which may carry remembered facts.
 *
 * @param {PlayerState} state
 * @returns {Transition}
 */
function quiet(state) {
  return { state, effects: [] };
}

/**
 * Whether the next try may start: waiting between tries, not for a reason (rows 23, I2).
 *
 * @param {PlayerState} state
 * @returns {boolean}
 */
function canStartTry(state) {
  return state.phase === "retrying" && !state.lossPending;
}

/**
 * Clears every timer except `keep`.
 *
 * @param {TimerName | null} keep
 * @returns {Effect[]}
 */
function clearTimers(keep) {
  /** @type {Effect[]} */
  const effects = [];
  for (const timer of TIMERS) if (timer !== keep) effects.push({ type: "clearTimer", timer });
  return effects;
}

/**
 * Leave: to a resting phase, with the events closed, the audio stopped and every timer
 * cancelled. The song goes with it (D73: no stale title).
 *
 * @param {"idle" | "ended" | "failed"} phase
 * @param {FailReason | null} reason
 * @returns {Transition}
 */
function leave(phase, reason) {
  return {
    state: { ...initialState(), phase, reason },
    effects: [{ type: "closeEvents" }, { type: "stopAudio" }, ...clearTimers(null)],
  };
}

/**
 * Retry: close the events, stop the audio, cancel every timer but the deadline, start the
 * deadline when no drop is running yet, then wait before the next try (D79b).
 *
 * @param {PlayerState} state
 * @param {number} deadlineMs how long the deadline runs, when this retry starts it
 * @returns {Transition}
 */
function retry(state, deadlineMs) {
  const attempt = state.attempt + 1;
  /** @type {Effect[]} */
  const effects = [{ type: "closeEvents" }, { type: "stopAudio" }, ...clearTimers("deadline")];
  if (!state.reconnecting) effects.push({ type: "setTimer", timer: "deadline", ms: deadlineMs });
  effects.push({ type: "setTimer", timer: "wait", ms: retryDelay(attempt) });
  return {
    state: {
      ...state,
      phase: "retrying",
      reason: null,
      song: null,
      titlesLost: false,
      lastStatus: null,
      attempt,
      reconnecting: true,
      lossPending: false,
      stalling: false,
    },
    effects,
  };
}

/**
 * A try that could not tune in: final on the first tune-in (D14, no tight loop), another try
 * on a reconnect (I3).
 *
 * @param {PlayerState} state
 * @returns {Transition}
 */
function tryFailed(state) {
  return state.reconnecting ? retry(state, DEADLINE_MS) : leave("failed", "unreachable");
}

/**
 * Rules 1 and 2 for the page's own lost audio (I2): `ended` or `unavailable` stops it, and
 * `stopped` reconnects at once. Any other reason decides nothing (`null`).
 *
 * @param {PlayerState} state
 * @param {StatusKind | null} kind
 * @param {number} deadlineMs
 * @returns {Transition | null}
 */
function decideByReason(state, kind, deadlineMs) {
  if (kind === "ended") return leave("ended", null);
  if (kind === "unavailable") return leave("failed", "unavailable");
  if (kind === "stopped") return retry(state, deadlineMs);
  return null;
}

/**
 * Row 1, and row 12 (the tap after a blocked autoplay plays within the gesture).
 *
 * @param {PlayerState} state
 * @returns {Transition}
 */
function onPlay(state) {
  if (isResting(state.phase)) {
    return {
      state: { ...initialState(), phase: "connecting" },
      effects: [{ type: "openEvents" }, { type: "setTimer", timer: "open", ms: OPEN_MS }],
    };
  }
  if (state.phase !== "needsTap") return quiet(state);
  return {
    state: { ...state, phase: "tuning" },
    effects: [
      { type: "clearTimer", timer: "tap" },
      { type: "resumeAudio" },
      { type: "setTimer", timer: "tune", ms: TUNE_MS },
    ],
  };
}

/**
 * Row 23: the next try opens the events again.
 *
 * @param {PlayerState} state
 * @returns {Transition}
 */
function startTry(state) {
  return {
    state: { ...state, phase: "connecting" },
    effects: [
      { type: "clearTimer", timer: "wait" },
      { type: "openEvents" },
      { type: "setTimer", timer: "open", ms: OPEN_MS },
    ],
  };
}

/**
 * Row 2: only the events' first `open` gives the audio its source (the ordering rule).
 *
 * @param {PlayerState} state
 * @returns {Transition}
 */
function onEventsOpen(state) {
  if (state.phase !== "connecting") return quiet(state);
  return {
    state: { ...state, phase: "tuning" },
    effects: [
      { type: "clearTimer", timer: "open" },
      { type: "startAudio" },
      { type: "setTimer", timer: "tune", ms: TUNE_MS },
    ],
  };
}

/**
 * Titles lost (row 22): the music carries on, with no song shown (D73).
 *
 * @param {PlayerState} state
 * @returns {Transition}
 */
function loseTitles(state) {
  return {
    state: { ...state, song: null, titlesLost: true },
    effects: [{ type: "closeEvents" }],
  };
}

/**
 * Rows 3, 4 and 22. A blip (`closed: false`) is left to the browser's own reconnect.
 *
 * @param {PlayerState} state
 * @param {boolean} closed
 * @returns {Transition}
 */
function onEventsFailed(state, closed) {
  if (!closed) return quiet(state);
  switch (state.phase) {
    case "tuning":
      // Coordinator ruling on the Task 3 review, choice (b): on a first tune-in the audio may
      // still play, so carry on without titles; the 15 s tune limit and the 2 s grace still
      // catch a real failure. A reconnect tries again, as before.
      return state.reconnecting ? tryFailed(state) : loseTitles(state);
    case "connecting":
    case "needsTap":
      return tryFailed(state);
    case "playing":
      return loseTitles(state);
    case "retrying":
      return { state, effects: [{ type: "closeEvents" }] };
    default:
      return quiet(state);
  }
}

/**
 * Rows 5-7: a status before the page's own audio plays. `tuning` means a new stream was just
 * admitted, so any song shown so far came from an older stream and goes (D73: no stale title).
 *
 * @param {PlayerState} state
 * @param {StatusKind} kind
 * @returns {Transition}
 */
function statusWhileTuning(state, kind) {
  switch (kind) {
    case "no_broadcast":
    case "unavailable":
      return leave("failed", kind);
    case "ended":
      return leave("ended", null);
    case "busy":
      return state.reconnecting ? retry(state, DEADLINE_MS) : leave("failed", "busy");
    case "stopped":
      return retry(state, DEADLINE_MS);
    case "tuning":
      return quiet({ ...state, song: null });
    default:
      return quiet(state);
  }
}

/**
 * Rows 5-7, 14 and 17.
 *
 * @param {PlayerState} state
 * @param {StatusKind} kind
 * @returns {Transition}
 */
function onStatus(state, kind) {
  switch (state.phase) {
    case "connecting":
    case "tuning":
    case "needsTap":
      return statusWhileTuning(state, kind);
    case "playing":
      // D78c: while its own audio plays, a status is information only.
      return quiet({ ...state, lastStatus: kind });
    case "retrying":
      if (!state.lossPending) return quiet(state);
      return decideByReason(state, kind, DEADLINE_MS) ?? quiet(state);
    default:
      return quiet(state);
  }
}

/**
 * Row 15.
 *
 * @param {PlayerState} state
 * @param {Song} song
 * @returns {Transition}
 */
function onNowPlaying(state, song) {
  const connected =
    state.phase === "tuning" || state.phase === "playing" || state.phase === "needsTap";
  if (!connected) return quiet(state);
  return quiet({ ...state, song, lastStatus: null });
}

/**
 * Rows 8 and 19. Playing again ends the drop: its deadline and its waits.
 *
 * @param {PlayerState} state
 * @returns {Transition}
 */
function onAudioPlaying(state) {
  if (state.phase === "playing" && state.stalling) {
    return {
      state: { ...state, stalling: false },
      effects: [{ type: "clearTimer", timer: "stall" }],
    };
  }
  if (state.phase !== "tuning") return quiet(state);
  return {
    state: {
      ...state,
      phase: "playing",
      lastStatus: null,
      attempt: 0,
      reconnecting: false,
      lossPending: false,
      stalling: false,
    },
    // Playing needs no timer: the tune limit, the grace and the deadline all end here.
    effects: clearTimers(null),
  };
}

/**
 * Rows 9 and 16. While tuning in, the audio's failure waits 2 s for a reason. While playing,
 * it is the drop: the remembered reason decides (rules 1-2), else the page waits 2 s for one
 * (rule 3), showing that it is reconnecting.
 *
 * @param {PlayerState} state
 * @returns {Transition}
 */
function onAudioLost(state) {
  if (state.phase === "tuning") {
    if (state.lossPending) return quiet(state);
    return {
      state: { ...state, lossPending: true },
      effects: [{ type: "setTimer", timer: "grace", ms: GRACE_MS }],
    };
  }
  if (state.phase !== "playing") return quiet(state);
  const decided = decideByReason(state, state.lastStatus, DEADLINE_MS);
  if (decided !== null) return decided;
  return {
    state: {
      ...state,
      phase: "retrying",
      song: null,
      lastStatus: null,
      reconnecting: true,
      lossPending: true,
      stalling: false,
    },
    effects: [
      { type: "clearTimer", timer: "stall" },
      { type: "setTimer", timer: "deadline", ms: DEADLINE_MS },
      { type: "setTimer", timer: "grace", ms: GRACE_MS },
    ],
  };
}

/**
 * Row 18: the stall watchdog starts on the first `waiting`.
 *
 * @param {PlayerState} state
 * @returns {Transition}
 */
function onAudioWaiting(state) {
  if (state.phase !== "playing" || state.stalling) return quiet(state);
  return {
    state: { ...state, stalling: true },
    effects: [{ type: "setTimer", timer: "stall", ms: STALL_MS }],
  };
}

/**
 * Row 11: autoplay was refused for the current try; the events stay open.
 *
 * @param {PlayerState} state
 * @returns {Transition}
 */
function onPlayBlocked(state) {
  if (state.phase !== "tuning") return quiet(state);
  return {
    state: { ...state, phase: "needsTap", lossPending: false },
    effects: [
      { type: "clearTimer", timer: "tune" },
      { type: "clearTimer", timer: "grace" },
      { type: "setTimer", timer: "tap", ms: TAP_MS },
    ],
  };
}

/**
 * Rows 4, 10, 13, 20, 23 and 24. A timer that runs out in a phase it does not belong to
 * changes nothing.
 *
 * @param {PlayerState} state
 * @param {TimerName} timer
 * @returns {Transition}
 */
function onTimeout(state, timer) {
  const { phase } = state;
  switch (timer) {
    case "open":
      return phase === "connecting" ? tryFailed(state) : quiet(state);
    case "tune":
      return phase === "tuning" ? tryFailed(state) : quiet(state);
    case "grace":
      if (phase === "tuning") return tryFailed(state);
      return phase === "retrying" && state.lossPending ? retry(state, DEADLINE_MS) : quiet(state);
    case "stall": {
      if (phase !== "playing") return quiet(state);
      // Rule 4: the stall has already waited; the deadline runs from its start.
      const deadlineMs = DEADLINE_MS - STALL_MS;
      return decideByReason(state, state.lastStatus, deadlineMs) ?? retry(state, deadlineMs);
    }
    case "tap":
      // A reconnect left untapped has lost the station (D79b), not merely stopped.
      if (phase !== "needsTap") return quiet(state);
      return state.reconnecting ? leave("failed", "lost") : leave("idle", null);
    case "deadline":
      return !isResting(phase) && state.reconnecting ? leave("failed", "lost") : quiet(state);
    case "wait":
      return canStartTry(state) ? startTry(state) : quiet(state);
  }
}

/**
 * The next state, and the effects that take the browser objects there.
 *
 * @param {PlayerState} state
 * @param {PlayerEvent} event
 * @returns {Transition}
 */
export function step(state, event) {
  switch (event.type) {
    case "play":
      return onPlay(state);
    case "stop":
      return isResting(state.phase) ? quiet(state) : leave("idle", null);
    case "visible":
      // A hidden tab's timers may be throttled: reconnect at once (design question 3).
      return canStartTry(state) ? startTry(state) : quiet(state);
    case "eventsOpen":
      return onEventsOpen(state);
    case "eventsFailed":
      return onEventsFailed(state, event.closed);
    case "status":
      return onStatus(state, event.kind);
    case "nowPlaying":
      return onNowPlaying(state, event.song);
    case "audioPlaying":
      return onAudioPlaying(state);
    case "audioLost":
      return onAudioLost(state);
    case "audioWaiting":
      return onAudioWaiting(state);
    case "audioPaused":
      // Row 21: a pause the page did not ask for (a call, unplugged headphones) is a Stop.
      return state.phase === "playing" ? leave("idle", null) : quiet(state);
    case "playBlocked":
      return onPlayBlocked(state);
    case "timeout":
      return onTimeout(state, event.timer);
  }
}
