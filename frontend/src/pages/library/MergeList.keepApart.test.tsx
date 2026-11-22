// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import type { ReactNode } from "react";

vi.mock("@/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/api/client")>();
  return { ...actual, apiFetch: vi.fn() };
});

import { ServerError, apiFetch } from "@/api/client";
import { MergeList } from "./MergeList";

// Artist model phase 3b PR 5c, SPEC D40, D55, D56 (scope check Q3, Q6, Q8, Q9, R4): Lance's lasting
// "Keep apart" mark on the merge list.
//
// - Each group has a "Keep apart" button beside its Skip tick. It keeps every pair of the group's
//   works apart (D55), with no confirm (Undo is there), then loads the same page again, keeping the
//   Skip ticks of the groups still shown.
// - A held group (two kept-apart works still joined through others, D56) shows a notice at its top
//   naming the kept pair and the works that join it, has no Skip tick, and Apply never sends it.
//   Its Keep apart button stays, so Lance can resolve it there.
// - "Kept apart", the last section, lists every pair the page sends (newest first, as sent): both
//   works, each linked to its work page with its credited artists, when (a <time>), the note, the
//   line "MusicBrainz links these works" where a work link joins them (Q6), and Undo, which undoes
//   that one pair and loads the page again. With no pairs there is no section.
// - A Keep apart that fails says so; while it runs its button is disabled.

const mockedApiFetch = vi.mocked(apiFetch);
const PAGE = "/api/v1/library/merge-list";
const APPLY = `${PAGE}/apply`;
const KEEP = `${PAGE}/keep-apart`;
const UNDO = `${PAGE}/kept-apart/undo`;
const LIMIT = 50;

function work(id: string, title: string, artists: string[], plays: number) {
  return {
    work_id: id,
    title,
    artists,
    plays,
    musicbrainz: false,
    master: { file_id: `file-${id}`, file_path: `D:/music/${id}.flac`, method: "auto" },
  };
}

const RUNAWAY = {
  target_id: "w-runaway",
  source_ids: ["w-runaway-2"],
  joins: ["shared_artist"],
  plays: 30,
  works: [work("w-runaway", "Runaway", ["Bon Jovi"], 30), work("w-runaway-2", "Runaway", [], 0)],
  flags: [],
  changes: [],
  failed: null,
  held: [],
};

const ALWAYS = {
  target_id: "w-studio",
  source_ids: ["w-live", "w-demo"],
  joins: ["shared_artist"],
  plays: 6,
  works: [
    work("w-studio", "Always", ["Bon Jovi"], 5),
    work("w-live", "Always (Live)", ["Bon Jovi"], 1),
    work("w-demo", "Always (Demo)", ["Bon Jovi"], 0),
  ],
  flags: [],
  changes: [],
  failed: null,
  held: [],
};

const HELD_ALWAYS = {
  ...ALWAYS,
  held: [{ first_work_id: "w-live", second_work_id: "w-studio" }],
};

const KEPT_MARMALADE = {
  first: { work_id: "w-aguilera", title: "Lady Marmalade", artists: ["Christina Aguilera"] },
  second: { work_id: "w-labelle", title: "Lady Marmalade", artists: ["LaBelle"] },
  kept_at: "2026-11-02T09:00:00Z",
  note: "Kept apart on the merge list",
  musicbrainz_links: true,
};

const KEPT_BED = {
  first: { work_id: "w-bed", title: "Bed Of Roses", artists: ["Bon Jovi"] },
  second: { work_id: "w-bed-live", title: "Bed Of Roses (Live)", artists: ["Bon Jovi"] },
  kept_at: "2026-11-21T09:00:00Z",
  note: "Split off bed-of-roses-live.mp3",
  musicbrainz_links: false,
};

let qc: QueryClient;
let served: Record<string, unknown>;

function wrapper({ children }: { children: ReactNode }) {
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  );
}

function offsetOf(url: string): number {
  return Number(new URL(url, "http://test").searchParams.get("offset"));
}

function isPageRead(url: string, init: RequestInit | undefined): boolean {
  return url.startsWith(`${PAGE}?`) && init?.method === undefined;
}

function pageRequests(): number {
  return mockedApiFetch.mock.calls.filter(([url, init]) => isPageRead(url, init)).length;
}

function writes(): [string, string, unknown][] {
  return mockedApiFetch.mock.calls
    .filter(([, init]) => (init as RequestInit | undefined)?.method !== undefined)
    .map(([url, init]) => {
      const options = init as RequestInit;
      return [url, options.method ?? "GET", options.body ? JSON.parse(String(options.body)) : null];
    });
}

type Write = (url: string, init: RequestInit) => Promise<unknown>;

const APPLIED = {
  results: [
    {
      target_id: "w-runaway",
      source_ids: ["w-runaway-2"],
      outcome: "merged",
      changes: [],
      error: null,
    },
  ],
  merged: 1,
  waiting: 0,
  failed: 0,
  already_merged: 0,
  changed: 0,
};

function defaultWrite(url: string): Promise<unknown> {
  if (url === KEEP) return Promise.resolve({ kept: [] });
  if (url === UNDO) return Promise.resolve(undefined);
  if (url === APPLY) return Promise.resolve(APPLIED);
  return Promise.resolve(undefined);
}

function serve(write: Write = defaultWrite) {
  mockedApiFetch.mockImplementation((url: string, init?: RequestInit) => {
    if (init?.method !== undefined) return write(url, init);
    if (url.startsWith(`${PAGE}?`))
      return Promise.resolve({ ...served, offset: offsetOf(url), limit: LIMIT });
    return Promise.resolve(undefined);
  });
}

function button(name: string): HTMLButtonElement {
  return screen.getByRole("button", { name }) as HTMLButtonElement;
}

beforeEach(() => {
  qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  mockedApiFetch.mockReset();
  served = {
    groups: [RUNAWAY, ALWAYS],
    duplicates: [],
    kept_works: [],
    kept_apart: [],
    total: 2,
  };
  serve();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("MergeList: Keep apart (D55)", () => {
  it("keeps every pair of a group's works apart, with no confirm, and loads the page again", async () => {
    render(<MergeList />, { wrapper });
    const always = await screen.findByRole("region", { name: "Merge into Always" });

    fireEvent.click(within(always).getByRole("button", { name: "Keep apart: Always" }));

    await waitFor(() => expect(writes()).toHaveLength(1));
    expect(writes()[0]).toEqual([KEEP, "POST", { work_ids: ["w-studio", "w-live", "w-demo"] }]);
    await waitFor(() => expect(pageRequests()).toBe(2));
    expect(
      mockedApiFetch.mock.calls
        .filter(([url, init]) => isPageRead(url, init))
        .map(([url]) => offsetOf(url))
    ).toEqual([0, 0]);
  });

  it("keeps the Skip ticks of the groups still shown", async () => {
    render(<MergeList />, { wrapper });
    await screen.findByRole("region", { name: "Merge into Always" });
    fireEvent.click(screen.getByRole("checkbox", { name: "Skip Runaway" }));
    served = { ...served, groups: [RUNAWAY], total: 1 };

    fireEvent.click(button("Keep apart: Always"));

    await waitFor(() =>
      expect(screen.queryByRole("region", { name: "Merge into Always" })).toBeNull()
    );
    expect(
      (screen.getByRole("checkbox", { name: "Skip Runaway" }) as HTMLInputElement).checked
    ).toBe(true);
  });

  it("says so when Keep apart fails, and disables the button while it runs", async () => {
    let fail: (reason: unknown) => void = () => {};
    serve((url) =>
      url === KEEP
        ? new Promise((_, reject) => {
            fail = reject;
          })
        : defaultWrite(url)
    );
    render(<MergeList />, { wrapper });
    await screen.findByRole("region", { name: "Merge into Always" });

    fireEvent.click(button("Keep apart: Always"));

    await waitFor(() => expect(button("Keep apart: Always").disabled).toBe(true));
    fail(new ServerError(500));
    expect((await screen.findByRole("alert")).textContent).toMatch(/keep apart/i);
    expect(button("Keep apart: Always").disabled).toBe(false);
  });
});

describe("MergeList: a held group (D56)", () => {
  beforeEach(() => {
    served = { ...served, groups: [RUNAWAY, HELD_ALWAYS] };
  });

  it("shows a notice naming the kept pair and the work that joins them", async () => {
    render(<MergeList />, { wrapper });
    const always = await screen.findByRole("region", { name: "Merge into Always" });

    const notice = within(always).getByRole("note");
    expect(notice.textContent).toMatch(/Not merged/);
    expect(notice.textContent).toContain("Always and Always (Live) are kept apart");
    expect(notice.textContent).toContain("Always (Demo)");
    expect(
      within(screen.getByRole("region", { name: "Merge into Runaway" })).queryByRole("note")
    ).toBeNull();
  });

  it("has no Skip tick but keeps its Keep apart button", async () => {
    render(<MergeList />, { wrapper });
    const always = await screen.findByRole("region", { name: "Merge into Always" });

    expect(within(always).queryByRole("checkbox", { name: "Skip Always" })).toBeNull();
    expect(within(always).getByRole("button", { name: "Keep apart: Always" })).toBeTruthy();
    expect(screen.getByRole("checkbox", { name: "Skip Runaway" })).toBeTruthy();
  });

  it("is never sent by Apply", async () => {
    render(<MergeList />, { wrapper });
    await screen.findByRole("region", { name: "Merge into Always" });

    fireEvent.click(button("Apply"));

    await waitFor(() => expect(writes()).toHaveLength(1));
    const [url, method, body] = writes()[0];
    expect([url, method]).toEqual([APPLY, "POST"]);
    expect(body).toEqual({
      groups: [{ target_id: "w-runaway", source_ids: ["w-runaway-2"], approvals: [] }],
    });
  });
});

describe("MergeList: the Kept apart section (Q9, Q3, Q6)", () => {
  it("lists every pair as sent: both works linked, their artists, when, the note", async () => {
    served = { ...served, kept_apart: [KEPT_BED, KEPT_MARMALADE] };
    render(<MergeList />, { wrapper });

    const section = await screen.findByRole("region", { name: "Kept apart" });
    const rows = within(section).getAllByRole("listitem");
    expect(rows).toHaveLength(2);
    const [bed, marmalade] = rows;
    expect(bed.textContent).toContain("Bed Of Roses");
    expect(bed.textContent).toContain("Bed Of Roses (Live)");
    expect(bed.textContent).toContain("Bon Jovi");
    expect(bed.textContent).toContain("Split off bed-of-roses-live.mp3");
    expect(bed.querySelector("time")?.getAttribute("datetime")).toBe("2026-11-21T09:00:00Z");
    expect(
      within(bed)
        .getAllByRole("link")
        .map((a) => a.getAttribute("href"))
    ).toEqual(["/library/works/w-bed", "/library/works/w-bed-live"]);
    expect(marmalade.textContent).toContain("Christina Aguilera");
    expect(marmalade.textContent).toContain("LaBelle");
    expect(marmalade.textContent).toContain("Kept apart on the merge list");
  });

  it("says where MusicBrainz links the pair", async () => {
    served = { ...served, kept_apart: [KEPT_BED, KEPT_MARMALADE] };
    render(<MergeList />, { wrapper });

    const section = await screen.findByRole("region", { name: "Kept apart" });
    const [bed, marmalade] = within(section).getAllByRole("listitem");
    expect(marmalade.textContent).toContain("MusicBrainz links these works");
    expect(bed.textContent).not.toContain("MusicBrainz links these works");
  });

  it("undoes one pair with Undo and loads the page again", async () => {
    served = { ...served, kept_apart: [KEPT_BED, KEPT_MARMALADE] };
    render(<MergeList />, { wrapper });
    await screen.findByRole("region", { name: "Kept apart" });

    fireEvent.click(button("Undo: Lady Marmalade and Lady Marmalade"));

    await waitFor(() => expect(writes()).toHaveLength(1));
    expect(writes()[0]).toEqual([
      UNDO,
      "POST",
      { first_work_id: "w-aguilera", second_work_id: "w-labelle" },
    ]);
    await waitFor(() => expect(pageRequests()).toBe(2));
  });

  it("shows no section when nothing is kept apart", async () => {
    render(<MergeList />, { wrapper });
    await screen.findByRole("region", { name: "Merge into Always" });

    expect(screen.queryByRole("region", { name: "Kept apart" })).toBeNull();
  });

  it("shows the pairs even when there is nothing to merge", async () => {
    served = { groups: [], duplicates: [], kept_works: [], kept_apart: [KEPT_BED], total: 0 };
    render(<MergeList />, { wrapper });

    expect(await screen.findByText(/Nothing to merge/)).toBeTruthy();
    expect(screen.getByRole("region", { name: "Kept apart" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Apply" })).toBeNull();
  });
});
