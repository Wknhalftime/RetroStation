// What the player page shows (module V). Every server string reaches the page through
// `textContent` only (XSS). Stub: typed surface only, filled in later.

/**
 * @typedef {{
 *   station: HTMLElement,
 *   title: HTMLElement,
 *   artist: HTMLElement,
 *   status: HTMLElement,
 *   note: HTMLElement,
 *   button: HTMLButtonElement
 * }} PlayerElements
 */

/**
 * @param {import("./player.js").PlayerView} view
 * @returns {string}
 */
export function statusMessage(view) {
  void view;
  return "";
}

/**
 * @param {import("./player.js").PlayerView} view
 * @returns {"Play" | "Stop"}
 */
export function buttonLabel(view) {
  void view;
  return "Play";
}

/**
 * @param {import("./urls.js").StationYearRef} station
 * @param {import("./player.js").PlayerView} view
 * @returns {string}
 */
export function documentTitle(station, view) {
  void station;
  void view;
  return "";
}

/**
 * @param {PlayerElements} elements
 * @param {import("./player.js").PlayerView} view
 * @returns {void}
 */
export function renderPlayer(elements, view) {
  void elements;
  void view;
}

/**
 * @param {PlayerElements} elements
 * @param {import("./urls.js").StationYearRef} station
 * @returns {void}
 */
export function renderStation(elements, station) {
  void elements;
  void station;
}

/**
 * @param {PlayerElements} elements
 * @param {boolean} saved
 * @returns {void}
 */
export function renderKeyNote(elements, saved) {
  void elements;
  void saved;
}
