// The player's behaviour and its browser bindings (modules M and C; design question 2).
// Stub: typed surface only, filled in later. `playerMachine.js` holds the state machine this
// module drives; it is private to this file.

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

/**
 * @param {PlayerDeps} deps
 * @returns {{play(): void, stop(): void, visible(): void, view(): PlayerView}}
 */
export function createPlayer(deps) {
  void deps;
  /** @type {PlayerView} */
  const resting = { phase: "idle", reason: null, song: null, titlesLost: false };
  return {
    play() {},
    stop() {},
    visible() {},
    view() {
      return resting;
    },
  };
}
