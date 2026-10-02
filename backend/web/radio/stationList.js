// The station-year list page (module S; F2 contract section 1). Renders the server's
// station-years, grouped under one heading per call sign, in the order the server already
// sorts them: call letters ignoring case, then year (D70, D71, D77).

import { STATION_YEARS_PATH, playerPath } from "./urls.js";

/**
 * @typedef {{
 *   callLetters: string,
 *   year: number,
 *   daysLogged: number,
 *   daysInYear: number
 * }} StationYearRow
 */

/**
 * @param {unknown} value
 * @returns {value is Record<string, unknown>}
 */
function isRecord(value) {
  return typeof value === "object" && value !== null;
}

/**
 * Narrows one entry of the response to a `StationYearRow`, or `null` when it does not match
 * the contract's shape.
 *
 * @param {unknown} entry
 * @returns {StationYearRow | null}
 */
function parseRow(entry) {
  if (!isRecord(entry)) return null;
  const callLetters = entry["call_letters"];
  const year = entry["year"];
  const daysLogged = entry["days_logged"];
  const daysInYear = entry["days_in_year"];
  if (typeof callLetters !== "string") return null;
  if (typeof year !== "number") return null;
  if (typeof daysLogged !== "number") return null;
  if (typeof daysInYear !== "number") return null;
  return { callLetters, year, daysLogged, daysInYear };
}

/**
 * Narrows `data` to the rows that match the contract's shape; a row that does not is
 * dropped.
 *
 * @param {unknown} data
 * @returns {StationYearRow[]}
 */
export function parseStationYears(data) {
  if (!Array.isArray(data)) return [];
  /** @type {StationYearRow[]} */
  const rows = [];
  for (const entry of data) {
    const row = parseRow(entry);
    if (row !== null) rows.push(row);
  }
  return rows;
}

/**
 * @param {(path: string) => Promise<{ok: boolean, json(): Promise<unknown>}>} fetchFn
 * @returns {Promise<StationYearRow[] | null>}
 */
export async function loadStationYears(fetchFn) {
  /** @type {{ok: boolean, json(): Promise<unknown>}} */
  let response;
  try {
    response = await fetchFn(STATION_YEARS_PATH);
  } catch (error) {
    if (error instanceof TypeError) return null;
    throw error;
  }
  if (!response.ok) return null;
  /** @type {unknown} */
  let data;
  try {
    data = await response.json();
  } catch (error) {
    // A body that is not JSON (`SyntaxError`), or one cut off while being read (`TypeError`,
    // as a failed fetch): either way the list could not be loaded.
    if (error instanceof SyntaxError || error instanceof TypeError) return null;
    throw error;
  }
  return parseStationYears(data);
}

/**
 * One station-year as a list item: a link to its player, followed by its logged-days text.
 * Every piece of text is a text node (XSS), never markup. `doc` is the container's own
 * document (D75: only `playerPage.js` and `listPage.js` read the global `document`).
 *
 * @param {Document} doc
 * @param {StationYearRow} row
 * @returns {HTMLLIElement}
 */
function renderRow(doc, row) {
  const item = doc.createElement("li");
  const link = doc.createElement("a");
  link.href = playerPath(row);
  link.textContent = String(row.year);
  item.appendChild(link);
  const days = `${row.daysLogged} of ${row.daysInYear} days`;
  item.appendChild(doc.createTextNode(` — ${days}`));
  return item;
}

/**
 * One call sign's heading and its years, in the order they arrived.
 *
 * @param {Document} doc
 * @param {string} callLetters
 * @param {StationYearRow[]} rows
 * @returns {HTMLElement}
 */
function renderGroup(doc, callLetters, rows) {
  const section = doc.createElement("section");
  const heading = doc.createElement("h2");
  heading.textContent = callLetters;
  section.appendChild(heading);
  const list = doc.createElement("ul");
  for (const row of rows) list.appendChild(renderRow(doc, row));
  section.appendChild(list);
  return section;
}

/**
 * @typedef {{callLetters: string, rows: StationYearRow[]}} CallSignGroup
 */

/**
 * Groups consecutive rows that share a call sign, ignoring case, as the server already
 * orders them.
 *
 * @param {StationYearRow[]} rows
 * @returns {CallSignGroup[]}
 */
function groupByCallSign(rows) {
  /** @type {CallSignGroup[]} */
  const groups = [];
  for (const row of rows) {
    const current = groups[groups.length - 1];
    const sameCallSign =
      current !== undefined && current.callLetters.toLowerCase() === row.callLetters.toLowerCase();
    if (current !== undefined && sameCallSign) {
      current.rows.push(row);
    } else {
      groups.push({ callLetters: row.callLetters, rows: [row] });
    }
  }
  return groups;
}

/**
 * @param {HTMLElement} container
 * @param {string} text
 * @returns {void}
 */
function renderMessage(container, text) {
  const message = container.ownerDocument.createElement("p");
  message.textContent = text;
  container.appendChild(message);
}

/**
 * @param {HTMLElement} container
 * @param {StationYearRow[] | null} rows
 * @returns {void}
 */
export function renderStationYears(container, rows) {
  container.replaceChildren();
  if (rows === null) {
    renderMessage(container, "The station list could not be loaded. Please try again later.");
    return;
  }
  if (rows.length === 0) {
    renderMessage(container, "No station-years are logged yet.");
    return;
  }
  const doc = container.ownerDocument;
  for (const group of groupByCallSign(rows)) {
    container.appendChild(renderGroup(doc, group.callLetters, group.rows));
  }
}
