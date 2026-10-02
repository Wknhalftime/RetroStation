// Entry point for the station-list page (D75: only this file and playerPage.js read
// `window` and the other browser globals).

import { loadStationYears, renderStationYears } from "./stationList.js";

/**
 * @returns {HTMLElement}
 */
function mainElement() {
  const element = document.querySelector("main");
  if (element === null) throw new Error("missing <main>");
  return element;
}

async function start() {
  const main = mainElement();
  renderStationYears(main, await loadStationYears((path) => fetch(path)));
}

start();
