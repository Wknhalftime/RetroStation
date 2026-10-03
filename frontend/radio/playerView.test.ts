// @vitest-environment jsdom
// Module V: what the player page shows (plan-f2.md Task 4; traceability-f2.md).
import { beforeEach, describe, expect, it } from "vitest";
import type { PlayerView } from "../../backend/web/radio/player.js";
import {
  buttonLabel,
  documentTitle,
  renderKeyNote,
  renderPlayer,
  renderStation,
  statusMessage,
} from "../../backend/web/radio/playerView.js";

const KIOA_1995 = { callLetters: "KIOA", year: 1995 };

function view(patch: Partial<PlayerView>): PlayerView {
  return { phase: "idle", reason: null, song: null, titlesLost: false, ...patch };
}

function makeElements() {
  const element = (tag: string, id: string) => {
    const node = document.createElement(tag);
    node.id = id;
    document.body.append(node);
    return node;
  };
  return {
    station: element("h1", "station"),
    title: element("p", "title"),
    artist: element("p", "artist"),
    status: element("p", "status"),
    note: element("p", "note"),
    button: document.body.appendChild(document.createElement("button")),
  };
}

/** A message a listener can read: words, not a code, a placeholder or markup. */
function readable(text: string | null): boolean {
  if (text === null) return false;
  return /[A-Za-z]{3}/.test(text) && !/undefined|null|NaN|\[object|[<>{}]/.test(text);
}

const PHASES: [string, Partial<PlayerView>, "Play" | "Stop"][] = [
  ["idle", { phase: "idle" }, "Play"],
  ["connecting", { phase: "connecting" }, "Stop"],
  ["tuning", { phase: "tuning" }, "Stop"],
  ["playing", { phase: "playing" }, "Stop"],
  ["playing with titlesLost", { phase: "playing", titlesLost: true }, "Stop"],
  ["retrying", { phase: "retrying" }, "Stop"],
  ["needsTap", { phase: "needsTap" }, "Play"],
  ["ended", { phase: "ended" }, "Play"],
  ["failed(no_broadcast)", { phase: "failed", reason: "no_broadcast" }, "Play"],
  ["failed(busy)", { phase: "failed", reason: "busy" }, "Play"],
  ["failed(unavailable)", { phase: "failed", reason: "unavailable" }, "Play"],
  ["failed(unreachable)", { phase: "failed", reason: "unreachable" }, "Play"],
  ["failed(lost)", { phase: "failed", reason: "lost" }, "Play"],
];

beforeEach(() => {
  document.body.replaceChildren();
  document.title = "";
});

describe("the player page", () => {
  // V1 — XSS (server strings reach the page as text only)
  it.each(["<img src=x onerror=alert(1)>", "Tom & Jerry", `"Quoted" 'song'`])(
    "names with markup are shown as text [%s]",
    (name) => {
      const elements = makeElements();
      const before = document.body.querySelectorAll("*").length;
      renderPlayer(elements, view({ phase: "playing", song: { artist: name, title: name } }));
      expect(elements.title.textContent).toBe(name);
      expect(elements.artist.textContent).toBe(name);
      expect(document.body.querySelectorAll("*").length).toBe(before);
      expect(document.querySelector("img")).toBeNull();
    }
  );

  // V2 — D78a; D42; D14; D79b; D73; D74. Copy is pinned only where the spec names it (audit N2):
  // "Lost the station" with a Play button (D79b), and Play / Stop (D74, D79b). Every other phase
  // shows its own readable message, distinct from every other phase's; while playing, the status
  // line shows nothing beside the song (D73), unless the titles are lost.
  it.each(PHASES)("each phase shows its plain message and button [%s]", (label, patch, button) => {
    const shown = view(patch);
    const message = statusMessage(shown);
    if (label === "playing") {
      expect(message).toBe("");
    } else {
      expect(readable(message)).toBe(true);
      const others = PHASES.filter(([other]) => other !== label);
      for (const [, other] of others) expect(statusMessage(view(other))).not.toBe(message);
    }
    if (label === "failed(lost)") expect(message).toContain("Lost the station");
    expect(buttonLabel(shown)).toBe(button);
    const elements = makeElements();
    renderPlayer(elements, shown);
    expect(elements.status.textContent).toBe(message);
    expect(elements.button.textContent).toBe(button);
  });

  // V3 — D73 (the artist and the title only; never a stale title)
  it("the song shows its title and artist only, replaced by the next", () => {
    const elements = makeElements();
    renderPlayer(
      elements,
      view({ phase: "playing", song: { artist: "Bon Jovi", title: "Always" } })
    );
    renderPlayer(
      elements,
      view({ phase: "playing", song: { artist: "Des'ree", title: "You Gotta Be" } })
    );
    expect(elements.title.textContent).toBe("You Gotta Be");
    expect(elements.artist.textContent).toBe("Des'ree");
    expect(document.body.textContent).not.toContain("Always");
    expect(document.body.textContent).not.toContain("Bon Jovi");
    renderPlayer(elements, view({ phase: "retrying" }));
    expect(elements.title.textContent).toBe("");
    expect(elements.artist.textContent).toBe("");
  });

  // V4 — design question 3 (the tab carries the song when MediaSession is missing, R2). The
  // wording is not pinned (audit N2): while playing the title names the song and its artist;
  // otherwise it names the station-year and not the song.
  it.each([
    ["playing", "playing" as const],
    ["not playing", "tuning" as const],
  ])("the document title follows the song [%s]", (label, phase) => {
    const song = { artist: "Edwyn Collins", title: "A Girl Like You" };
    const title = documentTitle(KIOA_1995, view({ phase, song }));
    expect(readable(title)).toBe(true);
    if (label === "playing") {
      expect(title).toContain("A Girl Like You");
      expect(title).toContain("Edwyn Collins");
    } else {
      expect(title).toContain("KIOA");
      expect(title).toContain("1995");
      expect(title).not.toContain("A Girl Like You");
    }
  });

  // V5 — D74 (storage refused: the place is kept for this page load only). The note's wording
  // is not pinned (audit N2).
  it("a key that could not be saved shows a note", () => {
    const elements = makeElements();
    renderKeyNote(elements, false);
    expect(elements.note.hidden).toBe(false);
    expect(readable(elements.note.textContent)).toBe(true);
    renderKeyNote(elements, true);
    expect(elements.note.hidden).toBe(true);
  });

  // V6 — D71
  it("the heading shows the station-year as text", () => {
    const elements = makeElements();
    renderStation(elements, KIOA_1995);
    expect(elements.station.textContent).toContain("KIOA");
    expect(elements.station.textContent).toContain("1995");
    expect(elements.station.children).toHaveLength(0);
  });
});
