// @vitest-environment jsdom
// Module S: the station-year list page (plan-f2.md Task 5; traceability-f2.md; F2 contract §1).
import { within } from "@testing-library/dom";
import { beforeEach, describe, expect, it } from "vitest";
import {
  loadStationYears,
  parseStationYears,
  renderStationYears,
} from "../../backend/web/radio/stationList.js";

interface FakeResponse {
  ok: boolean;
  json(): Promise<unknown>;
}

function serving(body: unknown) {
  const paths: string[] = [];
  const fetchFn = async (path: string): Promise<FakeResponse> => {
    paths.push(path);
    return { ok: true, json: async () => body };
  };
  return { paths, fetchFn };
}

function row(callLetters: string, year: number, daysLogged = 362, daysInYear = 365) {
  return { callLetters, year, daysLogged, daysInYear };
}

let container: HTMLElement;

beforeEach(() => {
  document.body.replaceChildren();
  container = document.body.appendChild(document.createElement("main"));
});

function links(): { href: string | null; text: string | null }[] {
  return [...container.querySelectorAll("a")].map((a) => ({
    href: a.getAttribute("href"),
    text: a.textContent,
  }));
}

interface YearGroup {
  heading: string | null;
  years: (string | null)[];
}

/** Each heading (by role) with the year links that follow it in document order. */
function yearsUnderHeadings(): YearGroup[] {
  const headings = within(container).queryAllByRole("heading");
  const groups: YearGroup[] = headings.map((heading) => ({
    heading: heading.textContent,
    years: [],
  }));
  const beforeAnyHeading: YearGroup = { heading: null, years: [] };
  for (const link of within(container).queryAllByRole("link")) {
    let owner: YearGroup = beforeAnyHeading;
    headings.forEach((heading, index) => {
      const follows = heading.compareDocumentPosition(link) & Node.DOCUMENT_POSITION_FOLLOWING;
      const group = groups[index];
      if (follows !== 0 && group !== undefined) owner = group;
    });
    owner.years.push(link.textContent);
  }
  return beforeAnyHeading.years.length === 0 ? groups : [beforeAnyHeading, ...groups];
}

/** A message a listener can read: words, not a code, a placeholder or markup. */
function readable(text: string | null): boolean {
  if (text === null) return false;
  return /[A-Za-z]{3}/.test(text) && !/undefined|null|NaN|\[object|[<>{}]/.test(text);
}

/** The text the list shows for `rows`, rendered into a separate element. */
function shownFor(rows: Parameters<typeof renderStationYears>[1]): string | null {
  const other = document.createElement("main");
  renderStationYears(other, rows);
  return other.textContent;
}

describe("the station-year list", () => {
  // S1 — D70 (days logged only); D71; D77 (no other count); contract §1
  it("each station-year shows its days logged and links to its player", async () => {
    const server = serving([
      { call_letters: "KIOA", year: 1995, days_logged: 362, days_in_year: 365 },
    ]);
    const rows = await loadStationYears(server.fetchFn);
    expect(server.paths).toEqual(["/radio/station-years"]);
    renderStationYears(container, rows);
    expect(links()).toEqual([{ href: "/radio/KIOA/1995", text: "1995" }]);
    expect(container.textContent).toContain("362 of 365 days");
    expect(container.textContent?.match(/\d+/g)).toEqual(["1995", "362", "365"]);
  });

  // S2 — contract §1 (the server's order: call letters ignoring case, then year). Asserted as a
  // listener sees it, through accessible roles: each call sign is one heading, and its years
  // are the links that follow it, in order. The markup is not pinned (coordinator ruling).
  it("rows keep the server's order, under each call sign", () => {
    renderStationYears(container, [row("Kazr", 2001), row("KIOA", 1995), row("KIOA", 1996)]);
    expect(yearsUnderHeadings()).toEqual([
      { heading: "Kazr", years: ["2001"] },
      { heading: "KIOA", years: ["1995", "1996"] },
    ]);
    expect(links().map((link) => link.href)).toEqual([
      "/radio/Kazr/2001",
      "/radio/KIOA/1995",
      "/radio/KIOA/1996",
    ]);
  });

  // S3 — contract §1 (`[]` when nothing is logged). The wording is not pinned (audit N2): a
  // readable message, no link, and not the failed-load message.
  it("an empty library says so", () => {
    renderStationYears(container, []);
    expect(readable(container.textContent)).toBe(true);
    expect(container.querySelectorAll("a")).toHaveLength(0);
    expect(container.textContent).not.toBe(shownFor(null));
  });

  // S4 — error handling (a failed load is told, never thrown). The wording is not pinned (audit
  // N2): a readable message, not the empty-library one.
  it.each([
    ["the fetch rejects", async (): Promise<FakeResponse> => Promise.reject(new TypeError("down"))],
    ["ok: false", async (): Promise<FakeResponse> => ({ ok: false, json: async () => [] })],
    [
      "json() rejects",
      async (): Promise<FakeResponse> => ({
        ok: true,
        json: async () => Promise.reject(new SyntaxError("Unexpected token")),
      }),
    ],
  ])("a failed load says so [%s]", async (_label, fetchFn) => {
    const rows = await loadStationYears(fetchFn);
    expect(rows).toBeNull();
    renderStationYears(container, rows);
    expect(readable(container.textContent)).toBe(true);
    expect(container.querySelectorAll("a")).toHaveLength(0);
    expect(container.textContent).not.toBe(shownFor([]));
  });

  // S5 — XSS; D72 (the stored casing and characters, encoded in the link)
  it("call letters with markup are text, and the link is encoded", () => {
    renderStationYears(container, [row("<b>K</b>", 1995)]);
    expect(within(container).getByRole("heading").textContent).toBe("<b>K</b>");
    expect(container.querySelector("b")).toBeNull();
    expect(links()).toEqual([{ href: "/radio/%3Cb%3EK%3C%2Fb%3E/1995", text: "1995" }]);
  });

  // S6 — CLAUDE.md (narrow `unknown`: a row not in the contract's shape is dropped)
  it.each([
    ["missing days_logged", { call_letters: "KXXX", year: 2001, days_in_year: 365 }],
    ["a string year", { call_letters: "KXXX", year: "2001", days_logged: 3, days_in_year: 365 }],
  ])("a row not in the contract's shape is skipped [%s]", (_label, bad) => {
    const good = { call_letters: "KIOA", year: 1995, days_logged: 362, days_in_year: 365 };
    expect(parseStationYears([good, bad, good])).toEqual([
      row("KIOA", 1995, 362, 365),
      row("KIOA", 1995, 362, 365),
    ]);
  });
});
