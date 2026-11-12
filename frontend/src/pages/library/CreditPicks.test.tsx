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
import { CreditPicks } from "./CreditPicks";

// Performances phase 3a PR 3, SPEC S9 (C57, C64, C65; D7): the Credit picks page. The first list
// is every proposal Apply takes, as the server sorts them, each with its line, log name, plays,
// what it plays now and what it would play; Lance ticks only the ones to skip and presses Apply
// once, which sends the ids he saw minus his ticks as `apply` and his ticks as `skip`, then says
// how many were applied, skipped and found stale, and loads the lists again. The second list,
// Version lines, has a pick per line: one candidate (its credit and what it would play) or None,
// stored at once. Loading, empty, error and in-flight states; a refused write says why. No
// button on the page starts with "Merge".

const mockedApiFetch = vi.mocked(apiFetch);
const PAGE = "/api/v1/library/credit-picks";
const APPLY = "/api/v1/library/credit-picks/apply";
const DUET = "00000000-0000-4000-8000-0000000000d1";
const LIVE = "00000000-0000-4000-8000-0000000000d2";

function file(name: string, source: string | null = null) {
  return {
    file_id: `file-${name}`,
    file_path: `D:/music/${name}`,
    track_title: null,
    file_status: "present",
    source,
  };
}

function item(id: string, title: string, artist: string, overrides: Record<string, unknown> = {}) {
  return {
    id,
    identity_id: `line-${id}`,
    kind: "credit",
    reason: "credit",
    state: "proposed",
    title,
    artist_name: artist,
    plays: 10,
    work_id: "w-home",
    work_title: "Who Says You Can't Go Home",
    performance_id: DUET,
    plays_now: file("solo.flac", "master"),
    would_play: file("duet.flac"),
    candidates: [] as unknown[],
    ...overrides,
  };
}

const HOME = item("p-home", "Who Says You Can't Go Home", "BON JOVI/JENNIFER NETTLES", {
  plays: 456,
});
const FREAK = item("p-freak", "Freak On A Leash", "KORN/AMY LEE", {
  plays: 12,
  work_title: "Freak On A Leash",
  plays_now: file("freak.flac", "master"),
  would_play: file("freak-duet.mp3"),
});
const SHARED = item("p-shared", "Cold Hard Truth", "JENNIFER NETTLES/KID ROCK", {
  reason: "shared_artist",
  plays: 3,
  would_play: file("cold-duet.flac"),
});
const LIVE_LINE = item("p-live", "Who Says You Can't Go Home (Live)", "BON JOVI/JENNIFER NETTLES", {
  kind: "version_line",
  performance_id: null,
  would_play: null,
  plays: 7,
  candidates: [
    {
      performance_id: DUET,
      credit_names: ["Bon Jovi", "Jennifer Nettles"],
      would_play: file("duet.flac"),
    },
    {
      performance_id: LIVE,
      credit_names: ["Bon Jovi", "Jennifer Nettles"],
      would_play: file("live.flac"),
    },
  ],
});

let qc: QueryClient;
let served: { credit: unknown[]; version_lines: unknown[]; total: number };

function wrapper({ children }: { children: ReactNode }) {
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  );
}

function pageRequests(): number {
  return mockedApiFetch.mock.calls.filter(
    ([url, init]) => url === PAGE && (init as RequestInit | undefined)?.method === undefined
  ).length;
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

const answer: Write = (url) =>
  url === APPLY
    ? Promise.resolve({ applied: 1, skipped: 1, stale: [] })
    : Promise.resolve({ performance_id: null });

function serve(write: Write = answer) {
  mockedApiFetch.mockImplementation((url: string, init?: RequestInit) => {
    if (init?.method !== undefined) return write(url, init);
    if (url === PAGE) return Promise.resolve(served);
    return Promise.resolve(undefined);
  });
}

beforeEach(() => {
  qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  mockedApiFetch.mockReset();
  served = { credit: [HOME, FREAK, SHARED], version_lines: [LIVE_LINE], total: 4 };
  serve();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("CreditPicks: the list Apply takes", () => {
  it("lists each proposal as served, with its line, plays and both files", async () => {
    render(<CreditPicks />, { wrapper });

    expect(await screen.findByRole("heading", { name: "Credit picks" })).toBeTruthy();
    const table = screen.getByRole("table", { name: "Credit picks to apply" });
    const rows = within(table).getAllByRole("row", { name: /\(/ });
    expect(rows.map((row) => row.getAttribute("aria-label"))).toEqual([
      "Who Says You Can't Go Home (BON JOVI/JENNIFER NETTLES)",
      "Freak On A Leash (KORN/AMY LEE)",
      "Cold Hard Truth (JENNIFER NETTLES/KID ROCK)",
    ]);
    const home = rows[0];
    expect(home.textContent).toContain("456");
    expect(home.textContent).toContain("solo.flac");
    expect(home.textContent).toContain("duet.flac");
    expect(within(rows[2]).getByText(/shared artist/i)).toBeTruthy();
    expect(within(rows[0]).queryByText(/shared artist/i)).toBeNull();
  });

  it("applies what was seen, minus the ticked skips, in one request", async () => {
    render(<CreditPicks />, { wrapper });
    await screen.findByRole("row", { name: /Freak On A Leash/ });

    fireEvent.click(screen.getByRole("checkbox", { name: "Skip Freak On A Leash" }));
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));

    await waitFor(() =>
      expect(writes()).toEqual([
        [APPLY, "POST", { apply: ["p-home", "p-shared"], skip: ["p-freak"] }],
      ])
    );
  });

  it("applies every row when nothing is ticked, and a tick can be taken back", async () => {
    render(<CreditPicks />, { wrapper });
    await screen.findByRole("row", { name: /Freak On A Leash/ });
    const skip = screen.getByRole("checkbox", { name: "Skip Freak On A Leash" });

    fireEvent.click(skip);
    fireEvent.click(skip);
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));

    await waitFor(() =>
      expect(writes()).toEqual([
        [APPLY, "POST", { apply: ["p-home", "p-freak", "p-shared"], skip: [] }],
      ])
    );
  });

  it("says what Apply did, stale rows included, and loads the lists again", async () => {
    serve((url, init) =>
      url === APPLY
        ? Promise.resolve({ applied: 2, skipped: 0, stale: ["p-shared"] })
        : answer(url, init)
    );
    render(<CreditPicks />, { wrapper });
    await screen.findByRole("row", { name: /Freak On A Leash/ });
    const before = pageRequests();

    fireEvent.click(screen.getByRole("button", { name: "Apply" }));

    const said = await screen.findByRole("status");
    expect(said.textContent).toMatch(/2 applied/);
    expect(said.textContent).toMatch(/1 stale/);
    await waitFor(() => expect(pageRequests()).toBeGreaterThan(before));
  });

  it("waits while Apply is in flight", async () => {
    let finish: () => void = () => {};
    serve(
      () =>
        new Promise((resolve) => {
          finish = () => resolve({ applied: 3, skipped: 0, stale: [] });
        })
    );
    render(<CreditPicks />, { wrapper });
    await screen.findByRole("row", { name: /Freak On A Leash/ });
    const button = screen.getByRole("button", { name: "Apply" }) as HTMLButtonElement;

    fireEvent.click(button);

    await waitFor(() => expect(button.disabled).toBe(true));
    fireEvent.click(button);
    expect(writes()).toHaveLength(1);
    finish();
    await waitFor(() => expect(button.disabled).toBe(false));
  });

  it("says why Apply was refused", async () => {
    serve(() => Promise.reject(new ConflictError("Proposal p-home is a version line")));
    render(<CreditPicks />, { wrapper });
    await screen.findByRole("row", { name: /Freak On A Leash/ });

    fireEvent.click(screen.getByRole("button", { name: "Apply" }));

    expect((await screen.findByRole("alert")).textContent).toContain(
      "Proposal p-home is a version line"
    );
  });

  it("offers no Apply when only version lines are left", async () => {
    served = { credit: [], version_lines: [LIVE_LINE], total: 1 };
    render(<CreditPicks />, { wrapper });

    await screen.findByRole("row", { name: /\(Live\)/ });
    expect(screen.queryByRole("button", { name: "Apply" })).toBeNull();
  });

  it("has no button that starts with Merge", async () => {
    render(<CreditPicks />, { wrapper });
    await screen.findByRole("row", { name: /Freak On A Leash/ });

    expect(screen.queryAllByRole("button", { name: /^Merge/ })).toHaveLength(0);
  });
});

describe("CreditPicks: version lines", () => {
  it("lists each version line with its candidates and None", async () => {
    render(<CreditPicks />, { wrapper });

    expect(await screen.findByRole("heading", { name: "Version lines" })).toBeTruthy();
    const row = screen.getByRole("row", {
      name: "Who Says You Can't Go Home (Live) (BON JOVI/JENNIFER NETTLES)",
    });
    const picks = within(row).getAllByRole("radio");
    expect(picks.map((radio) => radio.getAttribute("aria-label"))).toEqual([
      "Bon Jovi & Jennifer Nettles (duet.flac)",
      "Bon Jovi & Jennifer Nettles (live.flac)",
      "None",
    ]);
    expect(picks.every((radio) => !(radio as HTMLInputElement).checked)).toBe(true);
    expect(row.textContent).toContain("7");
  });

  it("stores a pick at once, and None as null", async () => {
    render(<CreditPicks />, { wrapper });
    await screen.findByRole("row", { name: /\(Live\)/ });

    fireEvent.click(screen.getByRole("radio", { name: "Bon Jovi & Jennifer Nettles (live.flac)" }));
    await waitFor(() => expect(writes()).toHaveLength(1));
    fireEvent.click(screen.getByRole("radio", { name: "None" }));

    await waitFor(() =>
      expect(writes()).toEqual([
        [`${PAGE}/p-live/pick`, "PUT", { performance_id: LIVE }],
        [`${PAGE}/p-live/pick`, "PUT", { performance_id: null }],
      ])
    );
  });

  it("shows a picked line's pick", async () => {
    served = {
      credit: [],
      version_lines: [{ ...LIVE_LINE, state: "picked", performance_id: LIVE }],
      total: 0,
    };
    render(<CreditPicks />, { wrapper });

    const live = await screen.findByRole("radio", {
      name: "Bon Jovi & Jennifer Nettles (live.flac)",
    });
    expect((live as HTMLInputElement).checked).toBe(true);
  });

  it("shows None checked for a line picked with none", async () => {
    served = {
      credit: [],
      version_lines: [{ ...LIVE_LINE, state: "picked", performance_id: null }],
      total: 0,
    };
    render(<CreditPicks />, { wrapper });

    const none = await screen.findByRole("radio", { name: "None" });
    expect((none as HTMLInputElement).checked).toBe(true);
  });

  it("says why a pick was refused", async () => {
    serve(() => Promise.reject(new ConflictError("That performance is now the default")));
    render(<CreditPicks />, { wrapper });
    await screen.findByRole("row", { name: /\(Live\)/ });

    fireEvent.click(screen.getByRole("radio", { name: "Bon Jovi & Jennifer Nettles (duet.flac)" }));

    expect((await screen.findByRole("alert")).textContent).toContain(
      "That performance is now the default"
    );
  });
});

describe("CreditPicks: loading, empty and error", () => {
  it("shows neither rows nor the empty note while it loads", async () => {
    mockedApiFetch.mockImplementation(() => new Promise<never>(() => {}));
    render(<CreditPicks />, { wrapper });

    await waitFor(() => expect(pageRequests()).toBe(1));
    expect(screen.queryAllByRole("row")).toHaveLength(0);
    expect(screen.queryByText(/Nothing to pick/)).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("says so when there is nothing to pick", async () => {
    served = { credit: [], version_lines: [], total: 0 };
    render(<CreditPicks />, { wrapper });

    expect(await screen.findByText(/Nothing to pick/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Apply" })).toBeNull();
  });

  it("says so when the lists fail to load", async () => {
    mockedApiFetch.mockRejectedValue(new ServerError(500));
    render(<CreditPicks />, { wrapper });

    expect((await screen.findByRole("alert")).textContent).toMatch(/Failed to load credit picks/);
  });
});
