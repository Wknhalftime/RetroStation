// The player's state machine (plan-f2.md "internal design"; private to player.js). Stub:
// typed surface only, filled in later.

/**
 * @typedef {{
 *   phase: import("./player.js").Phase,
 *   reason: import("./player.js").FailReason | null,
 *   song: import("./player.js").Song | null,
 *   titlesLost: boolean,
 *   lastStatus: import("./player.js").StatusKind | null,
 *   attempt: number,
 *   reconnecting: boolean,
 *   lossPending: boolean
 * }} PlayerState
 */

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
  };
}

/**
 * @param {PlayerState} state
 * @param {unknown} event
 * @returns {PlayerState}
 */
export function step(state, event) {
  void event;
  return state;
}

/**
 * `retryDelay(n) = min(1000 * 2^(n-1), 30000)` ms (D79b).
 *
 * @param {number} attempt
 * @returns {number}
 */
export function retryDelay(attempt) {
  void attempt;
  return 0;
}
