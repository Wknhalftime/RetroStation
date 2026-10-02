// Entry point for the player page (D75: only this file and listPage.js read `window` and
// the other browser globals). It reads its station-year from the URL, binds the browser
// objects to `createPlayer`, and wires the button, tab-visibility changes and leaving the page.

import { listenerKey } from "./listenerKey.js";
import { createPlayer } from "./player.js";
import {
  buttonLabel,
  documentTitle,
  renderKeyNote,
  renderPlayer,
  renderStation,
} from "./playerView.js";
import { parsePlayerPath } from "./urls.js";

/**
 * `window.localStorage`, or `null` when reading it is refused (a `DOMException`, e.g. a
 * browser's private mode). The key is then made for this page load only (D74).
 *
 * @returns {import("./listenerKey.js").KeyStorage | null}
 */
function safeStorage() {
  try {
    return window.localStorage;
  } catch (error) {
    if (error instanceof DOMException) return null;
    throw error;
  }
}

/**
 * @param {string} id
 * @returns {HTMLElement}
 */
function elementById(id) {
  const element = document.getElementById(id);
  if (element === null) throw new Error(`missing element #${id}`);
  return element;
}

/**
 * @param {string} id
 * @returns {HTMLButtonElement}
 */
function buttonById(id) {
  const element = elementById(id);
  if (!(element instanceof HTMLButtonElement)) throw new Error(`#${id} is not a button`);
  return element;
}

/**
 * @param {string} id
 * @returns {HTMLAudioElement}
 */
function audioById(id) {
  const element = elementById(id);
  if (!(element instanceof HTMLAudioElement)) throw new Error(`#${id} is not audio`);
  return element;
}

/**
 * The lock screen, wired only where the browser supports it (some plain-http pages have no
 * secure context, R2).
 *
 * @returns {import("./player.js").PlayerDeps["media"]}
 */
function mediaDeps() {
  if (!("mediaSession" in navigator)) return null;
  return {
    session: navigator.mediaSession,
    makeMetadata: (init) => new MediaMetadata(init),
  };
}

/**
 * @returns {import("./playerView.js").PlayerElements}
 */
function pageElements() {
  return {
    station: elementById("station"),
    title: elementById("title"),
    artist: elementById("artist"),
    status: elementById("status"),
    note: elementById("note"),
    button: buttonById("play"),
  };
}

function main() {
  const elements = pageElements();
  const station = parsePlayerPath(window.location.pathname);
  if (station === null) {
    elements.status.textContent = "This is not a station-year.";
    elements.button.hidden = true;
    return;
  }
  renderStation(elements, station);

  const { key, saved } = listenerKey(safeStorage(), (count) =>
    crypto.getRandomValues(new Uint8Array(count))
  );
  renderKeyNote(elements, saved);

  const player = createPlayer({
    station,
    key,
    audio: audioById("audio"),
    openEvents: (path) => new EventSource(path),
    timers: {
      set: (callback, ms) => window.setTimeout(callback, ms),
      clear: (handle) => window.clearTimeout(handle),
    },
    render: (view) => {
      renderPlayer(elements, view);
      document.title = documentTitle(station, view);
    },
    media: mediaDeps(),
  });

  elements.button.addEventListener("click", () => {
    if (buttonLabel(player.view()) === "Play") player.play();
    else player.stop();
  });

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") player.visible();
  });

  // Leaving the page frees the events and the stream (its listener slot) at once, even when
  // the browser keeps the page for Back/Forward; Play on return resumes from the bookmark.
  window.addEventListener("pagehide", () => player.stop());
}

main();
