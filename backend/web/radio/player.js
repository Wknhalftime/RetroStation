// The player's behaviour and its browser bindings (modules M and C; design question 2). The
// state machine in `playerMachine.js` decides; this controller turns the browser objects'
// events into its inputs and performs its effects. Every browser object is injected.

import { initialState, step } from "./playerMachine.js";
import { eventsPath, streamPath } from "./urls.js";

/**
 * @typedef {"tuning" | "no_broadcast" | "busy" | "unavailable" | "ended"
 *   | "stopped"} StatusKind
 */

/**
 * @typedef {"idle" | "connecting" | "tuning" | "playing" | "retrying" | "needsTap" | "ended"
 *   | "failed"} Phase
 */

/** @typedef {"no_broadcast" | "busy" | "unavailable" | "unreachable" | "lost"} FailReason */

/** @typedef {{artist: string, title: string}} Song */

/**
 * @typedef {{
 *   phase: Phase,
 *   reason: FailReason | null,
 *   song: Song | null,
 *   titlesLost: boolean
 * }} PlayerView
 */

/**
 * The `EventSource` surface the player uses, shared by the real browser object and the
 * fakes, with no cast either way.
 *
 * @typedef {{
 *   readonly readyState: number,
 *   addEventListener(type: string, listener: (event: {data?: unknown}) => void): void,
 *   close(): void
 * }} EventSourceLike
 */

/**
 * The `<audio>` surface the player uses. `stopAudio` is `pause()`, then
 * `removeAttribute("src")`, then `load()` - never `src = ""`.
 *
 * @typedef {{
 *   readonly paused: boolean,
 *   readonly ended: boolean,
 *   src: string,
 *   play(): Promise<void>,
 *   pause(): void,
 *   load(): void,
 *   removeAttribute(name: string): void,
 *   addEventListener(type: string, listener: (event: Event) => void): void
 * }} AudioLike
 */

/**
 * @typedef {{
 *   set(callback: () => void, ms: number): number,
 *   clear(handle: number): void
 * }} TimersLike
 */

/**
 * The lock-screen (`navigator.mediaSession`) surface the player uses, feature-detected by
 * its caller.
 *
 * @typedef {{
 *   metadata: unknown,
 *   playbackState: string,
 *   setActionHandler(action: string, handler: (() => void) | null): void
 * }} MediaSessionLike
 */

/**
 * @typedef {{
 *   station: import("./urls.js").StationYearRef,
 *   key: string,
 *   audio: AudioLike,
 *   openEvents: (path: string) => EventSourceLike,
 *   timers: TimersLike,
 *   render: (view: PlayerView) => void,
 *   media: {
 *     session: MediaSessionLike,
 *     makeMetadata: (init: {title: string, artist: string}) => unknown
 *   } | null
 * }} PlayerDeps
 */

/** @typedef {import("./playerMachine.js").PlayerState} PlayerState */
/** @typedef {import("./playerMachine.js").PlayerEvent} PlayerEvent */
/** @typedef {import("./playerMachine.js").Effect} Effect */
/** @typedef {import("./playerMachine.js").TimerName} TimerName */

const CLOSED = 2; // EventSource.readyState CLOSED

/** @type {readonly string[]} */
const STATUS_KINDS = ["tuning", "no_broadcast", "busy", "unavailable", "ended", "stopped"];

/**
 * @param {unknown} value
 * @returns {value is StatusKind}
 */
function isStatusKind(value) {
  return typeof value === "string" && STATUS_KINDS.includes(value);
}

/**
 * A frame's JSON payload, or `null` when the data is not a string of JSON.
 *
 * @param {unknown} data
 * @returns {unknown}
 */
function parseFrame(data) {
  if (typeof data !== "string") return null;
  try {
    return JSON.parse(data);
  } catch (error) {
    if (error instanceof SyntaxError) return null;
    throw error;
  }
}

/**
 * A `status` frame's kind, or `null` for a malformed frame or an unknown kind.
 *
 * @param {unknown} data
 * @returns {StatusKind | null}
 */
function statusKindOf(data) {
  const frame = parseFrame(data);
  if (typeof frame !== "object" || frame === null || !("kind" in frame)) return null;
  const { kind } = frame;
  return isStatusKind(kind) ? kind : null;
}

/**
 * A `now_playing` frame's song, or `null` unless it has a string artist and a string title.
 *
 * @param {unknown} data
 * @returns {Song | null}
 */
function songOf(data) {
  const frame = parseFrame(data);
  if (typeof frame !== "object" || frame === null) return null;
  if (!("artist" in frame) || !("title" in frame)) return null;
  const { artist, title } = frame;
  return typeof artist === "string" && typeof title === "string" ? { artist, title } : null;
}

/**
 * Whether a `play()` rejection is the browser refusing autoplay.
 *
 * @param {unknown} reason
 * @returns {boolean}
 */
function isAutoplayRefusal(reason) {
  return (
    typeof reason === "object" &&
    reason !== null &&
    "name" in reason &&
    reason.name === "NotAllowedError"
  );
}

/**
 * @param {PlayerState} state
 * @returns {PlayerView}
 */
function viewOf(state) {
  return {
    phase: state.phase,
    reason: state.reason,
    song: state.song,
    titlesLost: state.titlesLost,
  };
}

/**
 * @param {PlayerView} a
 * @param {PlayerView} b
 * @returns {boolean}
 */
function sameView(a, b) {
  return (
    a.phase === b.phase &&
    a.reason === b.reason &&
    a.titlesLost === b.titlesLost &&
    a.song?.artist === b.song?.artist &&
    a.song?.title === b.song?.title
  );
}

/**
 * The lock screen's play state: `playing` only while the audio plays, `none` at rest.
 *
 * @param {Phase} phase
 * @returns {string}
 */
function playbackStateOf(phase) {
  if (phase === "playing") return "playing";
  if (phase === "idle" || phase === "ended" || phase === "failed") return "none";
  return "paused";
}

/**
 * The lock screen (MediaSession): the artist and the title only, and `null` whenever the page
 * shows no song (D73). Each action handler is set on its own, because some browsers throw a
 * `TypeError` for an action they do not support.
 *
 * @param {NonNullable<PlayerDeps["media"]>} media
 * @param {{play(): void, stop(): void}} commands
 * @returns {(view: PlayerView) => void}
 */
function bindLockScreen(media, commands) {
  /** @type {Array<[string, () => void]>} */
  const handlers = [
    ["play", () => commands.play()],
    ["pause", () => commands.stop()],
    ["stop", () => commands.stop()],
  ];
  for (const [action, handler] of handlers) {
    try {
      media.session.setActionHandler(action, handler);
    } catch (error) {
      if (!(error instanceof TypeError)) throw error;
    }
  }
  /** @type {Song | null} */
  let shownSong = null;
  return (view) => {
    media.session.playbackState = playbackStateOf(view.phase);
    if (view.song === null) {
      media.session.metadata = null;
    } else if (view.song !== shownSong) {
      const { title, artist } = view.song;
      media.session.metadata = media.makeMetadata({ title, artist });
    }
    shownSong = view.song;
  };
}

/**
 * @param {PlayerDeps} deps
 * @returns {{play(): void, stop(): void, visible(): void, view(): PlayerView}}
 */
export function createPlayer(deps) {
  const { audio, timers } = deps;
  let state = initialState();
  let shown = viewOf(state);
  /** @type {EventSourceLike | null} */
  let events = null;
  /** @type {Map<TimerName, number>} */
  const running = new Map();
  // Tags each `play()`: a settlement of any earlier one (another try's) is ignored.
  let playTag = 0;

  /** @param {PlayerEvent} event */
  function dispatch(event) {
    const next = step(state, event);
    state = next.state;
    for (const effect of next.effects) perform(effect);
    const view = viewOf(state);
    if (sameView(view, shown)) return;
    shown = view;
    show(view);
  }

  /** @param {Effect} effect */
  function perform(effect) {
    switch (effect.type) {
      case "openEvents":
        openEvents();
        return;
      case "closeEvents":
        closeEvents();
        return;
      case "startAudio":
        audio.src = streamPath(deps.station, deps.key);
        playAudio();
        return;
      case "resumeAudio":
        playAudio();
        return;
      case "stopAudio":
        stopAudio();
        return;
      case "setTimer":
        setTimer(effect.timer, effect.ms);
        return;
      case "clearTimer":
        clearTimer(effect.timer);
        return;
    }
  }

  function openEvents() {
    const source = deps.openEvents(eventsPath(deps.station, deps.key));
    events = source;
    /**
     * Only the current source reaches the machine; a closed one's frames are ignored.
     *
     * @param {(data: unknown) => PlayerEvent | null} toEvent
     * @returns {(frame: {data?: unknown}) => void}
     */
    const whileCurrent = (toEvent) => (frame) => {
      if (events !== source) return;
      const event = toEvent(frame.data);
      if (event !== null) dispatch(event);
    };
    source.addEventListener(
      "open",
      whileCurrent(() => ({ type: "eventsOpen" }))
    );
    source.addEventListener(
      "error",
      whileCurrent(() => ({ type: "eventsFailed", closed: source.readyState === CLOSED }))
    );
    source.addEventListener(
      "status",
      whileCurrent((data) => {
        const kind = statusKindOf(data);
        return kind === null ? null : { type: "status", kind };
      })
    );
    source.addEventListener(
      "now_playing",
      whileCurrent((data) => {
        const song = songOf(data);
        return song === null ? null : { type: "nowPlaying", song };
      })
    );
  }

  function closeEvents() {
    if (events === null) return;
    events.close();
    events = null;
  }

  function playAudio() {
    playTag += 1;
    const tag = playTag;
    audio.play().catch((reason) => {
      // A play() rejection. Only this try's autoplay refusal asks for a tap; an AbortError
      // (the page stopped the audio) or a failed source is left to the element's own events.
      if (tag === playTag && isAutoplayRefusal(reason)) dispatch({ type: "playBlocked" });
    });
  }

  // The browser's idiom for silencing the audio and freeing its stream: never `src = ""`.
  function stopAudio() {
    playTag += 1;
    audio.pause();
    audio.removeAttribute("src");
    audio.load();
  }

  /**
   * @param {TimerName} timer
   * @param {number} ms
   */
  function setTimer(timer, ms) {
    clearTimer(timer);
    const handle = timers.set(() => {
      running.delete(timer);
      dispatch({ type: "timeout", timer });
    }, ms);
    running.set(timer, handle);
  }

  /** @param {TimerName} timer */
  function clearTimer(timer) {
    const handle = running.get(timer);
    if (handle === undefined) return;
    timers.clear(handle);
    running.delete(timer);
  }

  audio.addEventListener("playing", () => dispatch({ type: "audioPlaying" }));
  audio.addEventListener("waiting", () => dispatch({ type: "audioWaiting" }));
  audio.addEventListener("error", () => dispatch({ type: "audioLost" }));
  audio.addEventListener("ended", () => dispatch({ type: "audioLost" }));
  // The `pause` a browser fires just before `ended` is the stream running out, not a listener.
  audio.addEventListener("pause", () => {
    if (!audio.ended) dispatch({ type: "audioPaused" });
  });

  const player = {
    play() {
      dispatch({ type: "play" });
    },
    stop() {
      dispatch({ type: "stop" });
    },
    visible() {
      dispatch({ type: "visible" });
    },
    view() {
      return { ...shown };
    },
  };

  const lockScreen = deps.media === null ? null : bindLockScreen(deps.media, player);
  /** @param {PlayerView} view */
  function show(view) {
    deps.render(view);
    if (lockScreen !== null) lockScreen(view);
  }
  show(shown);
  return player;
}
