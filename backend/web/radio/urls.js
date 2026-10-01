// The radio pages' same-origin paths (module U). Every path is relative, starting with "/",
// so the pages never name their own origin (D71).

export const STATION_YEARS_PATH = "/radio/station-years";

/** @typedef {{callLetters: string, year: number}} StationYearRef */

// `/radio/{call}/{year}`, with an optional trailing slash. The call segment is kept raw here
// and decoded separately below, so a malformed escape can be told apart from a bad shape.
const PLAYER_PATH_PATTERN = /^\/radio\/([^/]+)\/([0-9]+)\/?$/;

const MIN_YEAR = 1;
const MAX_YEAR = 9999;

/**
 * Reads a station-year out of a player path, `/radio/{call}/{year}` with an optional
 * trailing slash. `call` is percent-decoded; a malformed escape gives `null`. `year` is
 * digits within 1-9999.
 *
 * @param {string} pathname
 * @returns {StationYearRef | null}
 */
export function parsePlayerPath(pathname) {
  const match = PLAYER_PATH_PATTERN.exec(pathname);
  if (match === null) return null;
  const [, encodedCallLetters, yearText] = match;
  let callLetters;
  try {
    callLetters = decodeURIComponent(encodedCallLetters);
  } catch (error) {
    if (error instanceof URIError) return null;
    throw error;
  }
  const year = Number(yearText);
  if (year < MIN_YEAR || year > MAX_YEAR) return null;
  return { callLetters, year };
}

/**
 * @param {StationYearRef} station
 * @returns {string}
 */
export function playerPath(station) {
  const call = encodeURIComponent(station.callLetters);
  const year = encodeURIComponent(String(station.year));
  return `/radio/${call}/${year}`;
}

/**
 * @param {StationYearRef} station
 * @param {string} key
 * @returns {string}
 */
export function streamPath(station, key) {
  const call = encodeURIComponent(station.callLetters);
  const year = encodeURIComponent(String(station.year));
  return `/listen/${call}/${year}?key=${encodeURIComponent(key)}`;
}

/**
 * @param {StationYearRef} station
 * @param {string} key
 * @returns {string}
 */
export function eventsPath(station, key) {
  const call = encodeURIComponent(station.callLetters);
  const year = encodeURIComponent(String(station.year));
  return `/listen/${call}/${year}/events?key=${encodeURIComponent(key)}`;
}
