// The radio pages' same-origin paths (module U). Every path is relative, starting with "/",
// so the pages never name their own origin (D71). Stub: typed surface only, filled in later.

export const STATION_YEARS_PATH = "/radio/station-years";

/** @typedef {{callLetters: string, year: number}} StationYearRef */

/**
 * Reads a station-year out of a player path, `/radio/{call}/{year}` with an optional
 * trailing slash. `call` is percent-decoded; a malformed escape gives `null`. `year` is
 * digits within 1-9999.
 *
 * @param {string} pathname
 * @returns {StationYearRef | null}
 */
export function parsePlayerPath(pathname) {
  void pathname;
  return null;
}

/**
 * @param {StationYearRef} station
 * @returns {string}
 */
export function playerPath(station) {
  void station;
  return "";
}

/**
 * @param {StationYearRef} station
 * @param {string} key
 * @returns {string}
 */
export function streamPath(station, key) {
  void station;
  void key;
  return "";
}

/**
 * @param {StationYearRef} station
 * @param {string} key
 * @returns {string}
 */
export function eventsPath(station, key) {
  void station;
  void key;
  return "";
}
