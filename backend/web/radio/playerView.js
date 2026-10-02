// What the player page shows (module V). Every server string reaches the page through
// `textContent` only (XSS): titles come from music tags, so no string here is ever treated
// as markup.

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
 * The failure message for a `failed` phase; "" is unreachable (every `failed` view carries a
 * reason), kept only so the switch has no missing branch.
 *
 * @param {import("./player.js").FailReason | null} reason
 * @returns {string}
 */
function failMessage(reason) {
  switch (reason) {
    case "no_broadcast":
      return "No broadcast at this time.";
    case "busy":
      return "Station busy: every listener slot is taken.";
    case "unavailable":
      return "The radio is unavailable right now.";
    case "unreachable":
      return "Could not tune in.";
    case "lost":
      return "Lost the station. Press Play to try again.";
    case null:
      return "";
  }
}

/**
 * The status line's plain-language message. Pinned: "" while playing with titles, and
 * "Lost the station" inside the `failed(lost)` message (D79b); every other phase reads its
 * own message, distinct from every other phase's (D78a; D42; D14; D73).
 *
 * @param {import("./player.js").PlayerView} view
 * @returns {string}
 */
export function statusMessage(view) {
  switch (view.phase) {
    case "idle":
      return "Press Play to listen.";
    case "connecting":
      return "Connecting…";
    case "tuning":
      return "Tuning in…";
    case "playing":
      return view.titlesLost ? "Song titles are unavailable." : "";
    case "retrying":
      return "Signal lost. Reconnecting…";
    case "needsTap":
      return "Tap Play to start the sound.";
    case "ended":
      return "The station has signed off.";
    case "failed":
      return failMessage(view.reason);
  }
}

/**
 * The button's label: "Play" at rest or on failure, "Stop" whenever the player is doing
 * something it can be asked to abandon (D74, D79b pin exactly these two words).
 *
 * @param {import("./player.js").PlayerView} view
 * @returns {"Play" | "Stop"}
 */
export function buttonLabel(view) {
  switch (view.phase) {
    case "idle":
    case "needsTap":
    case "ended":
    case "failed":
      return "Play";
    case "connecting":
    case "tuning":
    case "playing":
    case "retrying":
      return "Stop";
  }
}

/**
 * The tab's title: the song while it plays (never a stale one once the song has gone, D73),
 * otherwise the station-year, so a backgrounded tab never looks abandoned.
 *
 * @param {import("./urls.js").StationYearRef} station
 * @param {import("./player.js").PlayerView} view
 * @returns {string}
 */
export function documentTitle(station, view) {
  if (view.phase === "playing" && view.song !== null) {
    return `${view.song.title} – ${view.song.artist}`;
  }
  return `${station.callLetters} ${station.year} · RetroStation Radio`;
}

/**
 * Shows the song (artist and title only, D73) and the status line and button for `view`.
 * Every piece of text is a text node (`textContent`), never markup (XSS).
 *
 * @param {PlayerElements} elements
 * @param {import("./player.js").PlayerView} view
 * @returns {void}
 */
export function renderPlayer(elements, view) {
  elements.title.textContent = view.song === null ? "" : view.song.title;
  elements.artist.textContent = view.song === null ? "" : view.song.artist;
  elements.status.textContent = statusMessage(view);
  elements.button.textContent = buttonLabel(view);
}

/**
 * The heading: the station-year as plain text, with no child element (D71).
 *
 * @param {PlayerElements} elements
 * @param {import("./urls.js").StationYearRef} station
 * @returns {void}
 */
export function renderStation(elements, station) {
  elements.station.textContent = `${station.callLetters} ${station.year}`;
}

/**
 * The storage note: shown with a readable message when the key could not be saved, hidden
 * once it is (D74).
 *
 * @param {PlayerElements} elements
 * @param {boolean} saved
 * @returns {void}
 */
export function renderKeyNote(elements, saved) {
  elements.note.hidden = saved;
  elements.note.textContent = saved ? "" : "This browser will not remember where you left off.";
}
