// The station-year list page (module S; F2 contract section 1). Stub: typed surface only,
// filled in later.

/**
 * @typedef {{
 *   callLetters: string,
 *   year: number,
 *   daysLogged: number,
 *   daysInYear: number
 * }} StationYearRow
 */

/**
 * Narrows `data` to the rows that match the contract's shape; a row that does not is
 * dropped.
 *
 * @param {unknown} data
 * @returns {StationYearRow[]}
 */
export function parseStationYears(data) {
  void data;
  return [];
}

/**
 * @param {(path: string) => Promise<{ok: boolean, json(): Promise<unknown>}>} fetchFn
 * @returns {Promise<StationYearRow[] | null>}
 */
export async function loadStationYears(fetchFn) {
  void fetchFn;
  return null;
}

/**
 * @param {HTMLElement} container
 * @param {StationYearRow[] | null} rows
 * @returns {void}
 */
export function renderStationYears(container, rows) {
  void container;
  void rows;
}
