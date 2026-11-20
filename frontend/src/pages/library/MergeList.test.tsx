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

import { ConflictError, ServerError, apiFetch } from "@/api/client";
import { MergeList } from "./MergeList";

// Artist model phase 3b PR 4, SPEC S2, D6, D15, D27, D37, E3 (C57, C58, C74): the merge list,
// 50 groups a page (D37). Every group on the page as the server sorts it, a region named by the
// work it merges into, showing its works (that one first, marked as the one kept), each with its
// artists and plays, how they join, its flags (shown only) and each air-play change a pin can't
// hold, by station and line, with a tick to apply it there, unticked. Lance ticks only the groups
// to skip and presses Apply once, which sends every group of the page minus his skips, each with
// the changes he ticked as approvals; it then says what happened to each group and loads the
// same page again. Previous and Next, beside Apply, move by 50 and say which groups of how many
// are shown; a new page starts with no ticks, and a page an Apply emptied steps back to the last
// page that has groups. The recordings still under two works and the empty works kept are listed
// below. Loading, empty, error, credits-not-ready and in-flight states; a refused Apply says why.

const mockedApiFetch = vi.mocked(apiFetch);
const PAGE = "/api/v1/library/merge-list";
const APPLY = "/api/v1/library/merge-list/apply";
const LIMIT = 50;
const CHANGE_TICK = "Apply KAAA: Lady Marmalade (LABELLE)";

function pageUrl(offset: number): string {
  return `${PAGE}?offset=${offset}&limit=${LIMIT}`;
}

function offsetOf(url: string): number {
  return Number(new URL(url, "http://test").searchParams.get("offset"));
}

function button(name: string): HTMLButtonElement {
  return screen.getByRole("button", { name }) as HTMLButtonElement;
}

function checkbox(name: string): HTMLInputElement {
  return screen.getByRole("checkbox", { name }) as HTMLInputElement;
}

function work(id: string, title: string, artists: string[], plays: number, musicbrainz = false) {
  return {
    work_id: id,
    title,
    artists,
    plays,
    musicbrainz,
    master: { file_id: `file-${id}`, file_path: `D:/music/${id}.flac`, method: "auto" },
  };
}

const CHANGE = {
  station_id: "st-kaaa",
  station: "KAAA",
  identity_id: "line-1",
  title: "Lady Marmalade",
  artist: "LABELLE",
  plays: 12,
  from_file_id: "file-labelle",
  from_path: "D:/music/labelle.flac",
  to_file_id: "file-ac",
  to_path: "D:/music/aguilera-ac.mp3",
};

const MARMALADE = {
  target_id: "w-labelle",
  source_ids: ["w-aguilera"],
  joins: ["work_link"],
  plays: 320,
  works: [
    work("w-labelle", "Lady Marmalade", ["LaBelle"], 300, true),
    work("w-aguilera", "Lady Marmalade (Moulin Rouge)", ["Christina Aguilera"], 20),
  ],
  flags: [
    {
      kind: "masters_meet",
      work_id: "w-aguilera",
      file_id: "file-w-aguilera",
      identity_id: null,
      text: "Hand-picked master meets another master",
    },
  ],
  changes: [CHANGE],
  failed: null,
};

const ALWAYS = {
  target_id: "w-always",
  source_ids: ["w-always-2"],
  joins: ["shared_artist"],
  plays: 40,
  works: [
    work("w-always", "Always", ["Bon Jovi"], 40),
    work("w-always-2", "Always", ["Bon Jovi"], 0),
  ],
  flags: [
    {
      kind: "rejection",
      work_id: "w-always-2",
      file_id: null,
      identity_id: "line-9",
      text: "ALWAYS (BON JOVI) rejected a copy",
    },
  ],
  changes: [],
  failed: { error: "work delete refused", logged_at: "2026-11-19T10:00:00Z" },
};

const RUNAWAY = {
  target_id: "w-runaway",
  source_ids: ["w-runaway-2"],
  joins: ["shared_name"],
  plays: 3,
  works: [
    work("w-runaway", "Runaway", ["Bon Jovi"], 3),
    work("w-runaway-2", "Runaway", ["Bon Jovi"], 0),
  ],
  flags: [],
  changes: [],
  failed: null,
};

let qc: QueryClient;
let served: Record<string, unknown>;
let pageOf: (offset: number) => Record<string, unknown>;

function wrapper({ children }: { children: ReactNode }) {
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  );
}

function isPageRead(url: string, init: RequestInit | undefined): boolean {
  return url.startsWith(`${PAGE}?`) && init?.method === undefined;
}

function pageRequests(): number {
  return mockedApiFetch.mock.calls.filter(([url, init]) => isPageRead(url, init)).length;
}

function pageOffsets(): number[] {
  return mockedApiFetch.mock.calls
    .filter(([url, init]) => isPageRead(url, init))
    .map(([url]) => offsetOf(url));
}

function writes(): [string, string, unknown][] {
  return mockedApiFetch.mock.calls
    .filter(([, init]) => (init as RequestInit | undefined)?.method !== undefined)
    .map(([url, init]) => {
      const options = init as RequestInit;
      return [url, options.method ?? "GET", options.body ? JSON.parse(String(options.body)) : null];
    });
}

function result(group: typeof MARMALADE | typeof ALWAYS | typeof RUNAWAY, outcome: string) {
  return {
    target_id: group.target_id,
    source_ids: group.source_ids,
    outcome,
    changes: outcome === "waiting" ? [CHANGE] : [],
    error: outcome === "failed" ? "work delete refused again" : null,
  };
}

const APPLIED = {
  results: [result(MARMALADE, "waiting"), result(ALWAYS, "failed"), result(RUNAWAY, "merged")],
  merged: 1,
  waiting: 1,
  failed: 1,
  already_merged: 0,
  changed: 0,
};

type Write = (url: string, init: RequestInit) => Promise<unknown>;

function serve(write: Write = () => Promise.resolve(APPLIED)) {
  mockedApiFetch.mockImplementation((url: string, init?: RequestInit) => {
    if (init?.method !== undefined) return write(url, init);
    if (url.startsWith(`${PAGE}?`)) return Promise.resolve(pageOf(offsetOf(url)));
    return Promise.resolve(undefined);
  });
}

beforeEach(() => {
  qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  mockedApiFetch.mockReset();
  served = {
    groups: [MARMALADE, ALWAYS, RUNAWAY],
    duplicates: [],
    kept_works: [],
    total: 3,
  };
  pageOf = (offset) => ({ ...served, offset, limit: LIMIT });
  serve();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("MergeList: the planned groups", () => {
  it("shows each group, named by the work it merges into, with its works and plays", async () => {
    render(<MergeList />, { wrapper });

    expect(await screen.findByRole("heading", { name: "Merge list" })).toBeTruthy();
    const groups = screen.getAllByRole("region", { name: /^Merge into / });
    expect(groups.map((g) => g.getAttribute("aria-label"))).toEqual([
      "Merge into Lady Marmalade",
      "Merge into Always",
      "Merge into Runaway",
    ]);
    const marmalade = groups[0];
    const works = within(marmalade).getAllByRole("listitem");
    expect(works[0].textContent).toContain("Lady Marmalade");
    expect(works[0].textContent).toContain("LaBelle");
    expect(works[0].textContent).toContain("300");
    expect(works[0].textContent).toMatch(/kept/i);
    expect(works[1].textContent).toContain("Lady Marmalade (Moulin Rouge)");
    expect(works[1].textContent).toContain("Christina Aguilera");
    expect(works[1].textContent).not.toMatch(/kept/i);
  });

  it("says how each group's works join", async () => {
    render(<MergeList />, { wrapper });

    expect(
      (await screen.findByRole("region", { name: "Merge into Lady Marmalade" })).textContent
    ).toMatch(/same MusicBrainz work/i);
    expect(screen.getByRole("region", { name: "Merge into Always" }).textContent).toMatch(
      /same artist/i
    );
    expect(screen.getByRole("region", { name: "Merge into Runaway" }).textContent).toMatch(
      /same name/i
    );
  });

  it("shows each flag on its own group only", async () => {
    render(<MergeList />, { wrapper });

    const marmalade = await screen.findByRole("region", { name: "Merge into Lady Marmalade" });
    expect(marmalade.textContent).toContain("Hand-picked master meets another master");
    const always = screen.getByRole("region", { name: "Merge into Always" });
    expect(always.textContent).toContain("ALWAYS (BON JOVI) rejected a copy");
    expect(always.textContent).not.toContain("Hand-picked master");
    expect(screen.getByRole("region", { name: "Merge into Runaway" }).textContent).not.toMatch(
      /rejected|Hand-picked/
    );
  });

  it("lists each change a pin can't hold by station and line, unticked", async () => {
    render(<MergeList />, { wrapper });

    const marmalade = await screen.findByRole("region", { name: "Merge into Lady Marmalade" });
    const tick = within(marmalade).getByRole("checkbox", {
      name: "Apply KAAA: Lady Marmalade (LABELLE)",
    }) as HTMLInputElement;
    expect(tick.checked).toBe(false);
    const row = tick.closest("tr");
    expect(row?.textContent).toContain("12");
    expect(row?.textContent).toContain("labelle.flac");
    expect(row?.textContent).toContain("aguilera-ac.mp3");
    expect(
      within(screen.getByRole("region", { name: "Merge into Runaway" })).queryAllByRole(
        "checkbox",
        { name: /^Apply / }
      )
    ).toHaveLength(0);
  });

  it("shows a group's last failed attempt", async () => {
    render(<MergeList />, { wrapper });

    const always = await screen.findByRole("region", { name: "Merge into Always" });
    expect(always.textContent).toContain("work delete refused");
    expect(screen.getByRole("region", { name: "Merge into Runaway" }).textContent).not.toMatch(
      /failed/i
    );
  });
});

describe("MergeList: Apply", () => {
  it("sends the groups seen minus skips, each with the changes ticked there", async () => {
    render(<MergeList />, { wrapper });
    await screen.findByRole("region", { name: "Merge into Runaway" });

    fireEvent.click(screen.getByRole("checkbox", { name: "Apply KAAA: Lady Marmalade (LABELLE)" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Skip Always" }));
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));

    await waitFor(() =>
      expect(writes()).toEqual([
        [
          APPLY,
          "POST",
          {
            groups: [
              {
                target_id: "w-labelle",
                source_ids: ["w-aguilera"],
                approvals: [{ station_id: "st-kaaa", identity_id: "line-1", file_id: "file-ac" }],
              },
              { target_id: "w-runaway", source_ids: ["w-runaway-2"], approvals: [] },
            ],
          },
        ],
      ])
    );
  });

  it("sends a group whose change is not ticked with no approval, and a tick can be taken back", async () => {
    render(<MergeList />, { wrapper });
    await screen.findByRole("region", { name: "Merge into Runaway" });
    const tick = screen.getByRole("checkbox", { name: "Apply KAAA: Lady Marmalade (LABELLE)" });

    fireEvent.click(tick);
    fireEvent.click(tick);
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));

    await waitFor(() => expect(writes()).toHaveLength(1));
    const [, , body] = writes()[0];
    expect((body as { groups: { approvals: unknown[] }[] }).groups.map((g) => g.approvals)).toEqual(
      [[], [], []]
    );
  });

  it("says what Apply did to each group, and loads the list again", async () => {
    render(<MergeList />, { wrapper });
    await screen.findByRole("region", { name: "Merge into Runaway" });
    const before = pageRequests();

    fireEvent.click(screen.getByRole("button", { name: "Apply" }));

    const said = await screen.findByRole("status");
    expect(said.textContent).toMatch(/1 merged/);
    expect(said.textContent).toMatch(/1 waiting/);
    expect(said.textContent).toMatch(/1 failed/);
    expect(said.textContent).toContain("work delete refused again");
    expect(said.textContent).toContain("Lady Marmalade");
    await waitFor(() => expect(pageRequests()).toBeGreaterThan(before));
  });

  it("waits while Apply is in flight", async () => {
    let finish: () => void = () => {};
    serve(
      () =>
        new Promise((resolve) => {
          finish = () => resolve(APPLIED);
        })
    );
    render(<MergeList />, { wrapper });
    await screen.findByRole("region", { name: "Merge into Runaway" });
    const button = screen.getByRole("button", { name: "Apply" }) as HTMLButtonElement;

    fireEvent.click(button);

    await waitFor(() => expect(button.disabled).toBe(true));
    fireEvent.click(button);
    expect(writes()).toHaveLength(1);
    finish();
    await waitFor(() => expect(button.disabled).toBe(false));
  });

  it("says why Apply was refused", async () => {
    serve(() => Promise.reject(new ConflictError("Credits not ready: run D1's step first")));
    render(<MergeList />, { wrapper });
    await screen.findByRole("region", { name: "Merge into Runaway" });

    fireEvent.click(screen.getByRole("button", { name: "Apply" }));

    expect((await screen.findByRole("alert")).textContent).toContain(
      "Credits not ready: run D1's step first"
    );
  });

  it("has no button that merges at once", async () => {
    render(<MergeList />, { wrapper });
    await screen.findByRole("region", { name: "Merge into Runaway" });

    expect(screen.queryAllByRole("button", { name: /^Merge/ })).toHaveLength(0);
    expect(writes()).toHaveLength(0);
  });
});

describe("MergeList: the other two sections", () => {
  it("lists the recordings still under two works and the empty works kept", async () => {
    served = {
      groups: [],
      duplicates: [
        {
          recording_mbid: "rec-0001",
          works: [
            { work_id: "w-1", title: "Always" },
            { work_id: "w-2", title: "Always (Live)" },
          ],
        },
      ],
      kept_works: [
        { work_id: "w-kept", title: "Lithium (old)", references: ["format_override", "match"] },
      ],
      total: 0,
    };
    render(<MergeList />, { wrapper });

    const duplicates = await screen.findByRole("region", { name: "Recordings under two works" });
    expect(duplicates.textContent).toContain("rec-0001");
    expect(duplicates.textContent).toContain("Always (Live)");
    const kept = screen.getByRole("region", { name: "Empty works kept" });
    expect(kept.textContent).toContain("Lithium (old)");
    expect(kept.textContent).toMatch(/format override/i);
    expect(kept.textContent).toMatch(/match/i);
    expect(screen.getByText(/Nothing to merge/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Apply" })).toBeNull();
  });
});

describe("MergeList: loading, empty and error", () => {
  it("shows neither groups nor the empty note while it loads", async () => {
    mockedApiFetch.mockImplementation(() => new Promise<never>(() => {}));
    render(<MergeList />, { wrapper });

    await waitFor(() => expect(pageRequests()).toBe(1));
    expect(screen.queryAllByRole("region")).toHaveLength(0);
    expect(screen.queryByText(/Nothing to merge/)).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("says so when there is nothing to merge", async () => {
    served = { groups: [], duplicates: [], kept_works: [], total: 0 };
    render(<MergeList />, { wrapper });

    expect(await screen.findByText(/Nothing to merge/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Apply" })).toBeNull();
  });

  it("says so when the list fails to load", async () => {
    mockedApiFetch.mockRejectedValue(new ServerError(500));
    render(<MergeList />, { wrapper });

    expect((await screen.findByRole("alert")).textContent).toMatch(/Failed to load the merge list/);
  });

  it("says why when the credits aren't ready", async () => {
    mockedApiFetch.mockRejectedValue(new ConflictError("Credits not ready: run D1's step first"));
    render(<MergeList />, { wrapper });

    expect((await screen.findByRole("alert")).textContent).toContain(
      "Credits not ready: run D1's step first"
    );
  });
});

describe("MergeList: pages of 50 (D37)", () => {
  it("asks for the first page and says which groups of how many it shows", async () => {
    served = { ...served, total: 120 };
    render(<MergeList />, { wrapper });
    await screen.findByRole("region", { name: "Merge into Runaway" });

    expect(mockedApiFetch.mock.calls[0][0]).toBe(pageUrl(0));
    expect(screen.getByText("Groups 1–3 of 120")).toBeTruthy();
    expect(button("Previous").disabled).toBe(true);
    expect(button("Next").disabled).toBe(false);
  });

  it("moves by 50 with Next and Previous", async () => {
    served = { ...served, total: 120 };
    render(<MergeList />, { wrapper });
    await screen.findByText("Groups 1–3 of 120");

    fireEvent.click(button("Next"));
    expect(await screen.findByText("Groups 51–53 of 120")).toBeTruthy();
    expect(pageOffsets()).toContain(50);
    fireEvent.click(button("Previous"));
    expect(await screen.findByText("Groups 1–3 of 120")).toBeTruthy();
  });

  it("offers neither Previous nor Next when one page holds every group", async () => {
    render(<MergeList />, { wrapper });
    await screen.findByText("Groups 1–3 of 3");

    expect(button("Previous").disabled).toBe(true);
    expect(button("Next").disabled).toBe(true);
  });

  it("starts a new page with no ticks", async () => {
    served = { ...served, total: 120 };
    render(<MergeList />, { wrapper });
    await screen.findByText("Groups 1–3 of 120");
    fireEvent.click(screen.getByRole("checkbox", { name: "Skip Always" }));
    fireEvent.click(screen.getByRole("checkbox", { name: CHANGE_TICK }));

    fireEvent.click(button("Next"));
    await screen.findByText("Groups 51–53 of 120");

    expect(checkbox("Skip Always").checked).toBe(false);
    expect(checkbox(CHANGE_TICK).checked).toBe(false);
  });

  it("sends only the page's groups and loads the same page again after Apply", async () => {
    served = { ...served, total: 120 };
    render(<MergeList />, { wrapper });
    await screen.findByText("Groups 1–3 of 120");
    fireEvent.click(button("Next"));
    await screen.findByText("Groups 51–53 of 120");
    const before = pageOffsets().length;

    fireEvent.click(button("Apply"));

    await screen.findByRole("status");
    expect(writes()).toHaveLength(1);
    expect((writes()[0][2] as { groups: unknown[] }).groups).toHaveLength(3);
    await waitFor(() => expect(pageOffsets().length).toBeGreaterThan(before));
    const offsets = pageOffsets();
    expect(offsets[offsets.length - 1]).toBe(50);
  });

  it("steps back to the last page with groups when an Apply empties its page", async () => {
    let applied = false;
    served = { ...served, total: 53 };
    pageOf = (offset) =>
      applied && offset >= LIMIT
        ? { ...served, groups: [], total: 50, offset, limit: LIMIT }
        : { ...served, total: applied ? 50 : 53, offset, limit: LIMIT };
    serve(() => {
      applied = true;
      return Promise.resolve(APPLIED);
    });
    render(<MergeList />, { wrapper });
    await screen.findByText("Groups 1–3 of 53");
    fireEvent.click(button("Next"));
    await screen.findByText("Groups 51–53 of 53");

    fireEvent.click(button("Apply"));

    expect(await screen.findByText("Groups 1–3 of 50")).toBeTruthy();
    const offsets = pageOffsets();
    expect(offsets[offsets.length - 1]).toBe(0);
  });
});
