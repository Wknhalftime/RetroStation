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
import { UnmappedVersions } from "./UnmappedVersions";

// Versions phase 2b PR 3, SPEC S7 (C62): the Unmapped versions page lists every unmapped version
// text, 50 a page, as the server sorts them (by plays), each with its work, artist, plays,
// stations, first seen, the New marker and any suggestion. Map maps a row to its suggestion, and
// a row without one is mapped on its work page (question B). Checkboxes pick rows for Map
// selected (each to its own suggestion) and Dismiss selected (question A). Dismiss dismisses one
// row; Dismiss all asks first, then dismisses every listed one on every page. Open work opens the
// row's work page. The buttons wait while a write is in flight, and a refused one says why.

const mockedApiFetch = vi.mocked(apiFetch);
const FILE = "00000000-0000-4000-8000-0000000000b1";
const OTHER_FILE = "00000000-0000-4000-8000-0000000000b2";
const DISMISS = "/api/v1/library/versions/dismiss";

function item(work: string, title: string, text: string, overrides: Record<string, unknown> = {}) {
  return {
    work_id: work,
    work_title: title,
    artist_id: "a-metallica",
    artist_name: "Metallica",
    version_text: text,
    version_type: "live",
    reads_as_version: true,
    plays: 10,
    stations: ["KISW"],
    first_seen: "2001-05-03",
    first_station: "KISW",
    new: false,
    suggestion: null as null | Record<string, unknown>,
    ...overrides,
  };
}

function suggestion(fileId: string, title: string) {
  return {
    file_id: fileId,
    reason: "only_copy",
    track_title: title,
    file_path: `D:/${fileId}.flac`,
  };
}

const ONE = item("w-one", "One", "live", {
  plays: 456,
  stations: ["KISW", "WXRT"],
  new: true,
  suggestion: suggestion(FILE, "One (Live in Seattle)"),
});
const FUEL = item("w-fuel", "Fuel", "demo", { version_type: "demo", plays: 12 });
const BATTERY = item("w-battery", "Battery", "lv", {
  plays: 9,
  suggestion: suggestion(OTHER_FILE, "Battery (Live)"),
});

function pageOf(items: unknown[], total = items.length, newTotal = 0) {
  return { items, total, new_total: newTotal };
}

let qc: QueryClient;
let served: ReturnType<typeof pageOf>;

function wrapper({ children }: { children: ReactNode }) {
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  );
}

/** offset and limit of each page request, in order. */
function pageRequests(): [string | null, string | null][] {
  return mockedApiFetch.mock.calls
    .map(([url]) => url)
    .filter((url) => url.startsWith("/api/v1/library/versions?"))
    .map((url) => {
      const params = new URLSearchParams(url.split("?")[1]);
      return [params.get("offset"), params.get("limit")];
    });
}

function writes(): [string, string, unknown][] {
  return mockedApiFetch.mock.calls
    .filter(([, init]) => (init as RequestInit | undefined)?.method !== undefined)
    .map(([url, init]) => {
      const options = init as RequestInit;
      return [url, options.method ?? "GET", options.body ? JSON.parse(String(options.body)) : null];
    });
}

function serve(write: () => Promise<unknown> = () => Promise.resolve(undefined)) {
  mockedApiFetch.mockImplementation((url: string, init?: RequestInit) => {
    if (init?.method === "POST" && url === DISMISS) return Promise.resolve({ dismissed: 1 });
    if (init?.method !== undefined) return write();
    if (url.startsWith("/api/v1/library/versions?")) return Promise.resolve(served);
    return Promise.resolve(undefined);
  });
}

beforeEach(() => {
  qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  mockedApiFetch.mockReset();
  served = pageOf([ONE, FUEL, BATTERY], 3, 1);
  serve();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("UnmappedVersions: the list", () => {
  it("lists each row as served, with its work, artist, plays, stations and suggestion", async () => {
    render(<UnmappedVersions />, { wrapper });

    expect(await screen.findByRole("heading", { name: "Unmapped versions" })).toBeTruthy();
    const rows = await screen.findAllByRole("row", { name: /One|Fuel|Battery/ });
    expect(rows.map((row) => row.getAttribute("aria-label"))).toEqual([
      "One (live)",
      "Fuel (demo)",
      "Battery (lv)",
    ]);
    const one = rows[0];
    expect(one.textContent).toContain("Metallica");
    expect(one.textContent).toContain("456");
    expect(one.textContent).toContain("KISW");
    expect(one.textContent).toContain("WXRT");
    expect(one.textContent).toContain("2001-05-03");
    expect(one.textContent).toContain("One (Live in Seattle)");
    expect(pageRequests()[0]).toEqual(["0", "50"]);
  });

  it("marks the new rows only", async () => {
    render(<UnmappedVersions />, { wrapper });

    const one = await screen.findByRole("row", { name: "One (live)" });
    expect(within(one).getByText("New")).toBeTruthy();
    expect(within(screen.getByRole("row", { name: "Fuel (demo)" })).queryByText("New")).toBeNull();
  });

  it("opens the row's work page", async () => {
    render(<UnmappedVersions />, { wrapper });

    const link = await screen.findByRole("link", { name: "Open One" });
    expect(link.getAttribute("href")).toBe("/library/artists/a-metallica/works/w-one");
  });

  it("shows neither rows nor the empty note while it loads", async () => {
    mockedApiFetch.mockImplementation(() => new Promise<never>(() => {}));
    render(<UnmappedVersions />, { wrapper });

    await waitFor(() => expect(pageRequests()).toHaveLength(1));
    expect(screen.queryAllByRole("row")).toHaveLength(0);
    expect(screen.queryByText(/No unmapped versions/)).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("says so when nothing is unmapped", async () => {
    served = pageOf([]);
    render(<UnmappedVersions />, { wrapper });

    expect(await screen.findByText(/No unmapped versions/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Dismiss all" })).toBeNull();
  });

  it("says so when the list fails to load", async () => {
    mockedApiFetch.mockRejectedValue(new ServerError(500));
    render(<UnmappedVersions />, { wrapper });

    expect((await screen.findByRole("alert")).textContent).toMatch(
      /Failed to load unmapped versions/
    );
  });

  it("turns the page 50 at a time", async () => {
    served = pageOf([ONE, FUEL], 120);
    render(<UnmappedVersions />, { wrapper });
    await screen.findByRole("row", { name: "One (live)" });

    expect((screen.getByRole("button", { name: "Previous" }) as HTMLButtonElement).disabled).toBe(
      true
    );
    fireEvent.click(screen.getByRole("button", { name: "Next" }));

    await waitFor(() => expect(pageRequests()).toContainEqual(["50", "50"]));
  });
});

describe("UnmappedVersions: the actions", () => {
  it("maps a row to its suggestion; a row without one offers no Map", async () => {
    render(<UnmappedVersions />, { wrapper });
    await screen.findByRole("row", { name: "One (live)" });

    expect(screen.queryByRole("button", { name: "Map demo on Fuel" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Map live on One" }));

    await waitFor(() =>
      expect(writes()).toEqual([
        [
          "/api/v1/library/works/w-one/version-entries",
          "POST",
          { version_texts: ["live"], file_id: FILE },
        ],
      ])
    );
  });

  it("maps each selected row to its own suggestion, skipping a row without one", async () => {
    render(<UnmappedVersions />, { wrapper });
    await screen.findByRole("row", { name: "One (live)" });
    const mapSelected = screen.getByRole("button", { name: "Map selected" }) as HTMLButtonElement;

    expect(mapSelected.disabled).toBe(true);
    fireEvent.click(screen.getByRole("checkbox", { name: "Select Fuel (demo)" }));
    expect(mapSelected.disabled).toBe(true);
    fireEvent.click(screen.getByRole("checkbox", { name: "Select One (live)" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Select Battery (lv)" }));
    fireEvent.click(mapSelected);

    await waitFor(() => expect(writes()).toHaveLength(2));
    expect(writes()).toEqual(
      expect.arrayContaining([
        [
          "/api/v1/library/works/w-one/version-entries",
          "POST",
          { version_texts: ["live"], file_id: FILE },
        ],
        [
          "/api/v1/library/works/w-battery/version-entries",
          "POST",
          { version_texts: ["lv"], file_id: OTHER_FILE },
        ],
      ])
    );
  });

  it("dismisses one row", async () => {
    render(<UnmappedVersions />, { wrapper });
    await screen.findByRole("row", { name: "Fuel (demo)" });

    fireEvent.click(screen.getByRole("button", { name: "Dismiss demo on Fuel" }));

    await waitFor(() =>
      expect(writes()).toEqual([
        ["/api/v1/library/works/w-fuel/versions/dismiss", "POST", { version_texts: ["demo"] }],
      ])
    );
  });

  it("dismisses the selected rows", async () => {
    render(<UnmappedVersions />, { wrapper });
    await screen.findByRole("row", { name: "One (live)" });
    const selected = screen.getByRole("button", { name: "Dismiss selected" }) as HTMLButtonElement;

    expect(selected.disabled).toBe(true);
    fireEvent.click(screen.getByRole("checkbox", { name: "Select One (live)" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Select Fuel (demo)" }));
    fireEvent.click(selected);

    await waitFor(() =>
      expect(writes()).toEqual([
        [
          DISMISS,
          "POST",
          {
            entries: [
              { work_id: "w-one", version_text: "live" },
              { work_id: "w-fuel", version_text: "demo" },
            ],
          },
        ],
      ])
    );
  });

  it("asks before Dismiss all, naming every listed one, then dismisses them all", async () => {
    served = pageOf([ONE, FUEL], 120, 1);
    const confirm = vi
      .spyOn(window, "confirm")
      .mockReturnValueOnce(false)
      .mockReturnValueOnce(true);
    render(<UnmappedVersions />, { wrapper });
    await screen.findByRole("row", { name: "One (live)" });

    fireEvent.click(screen.getByRole("button", { name: "Dismiss all" }));
    expect(confirm).toHaveBeenCalledTimes(1);
    expect(writes()).toEqual([]);
    fireEvent.click(screen.getByRole("button", { name: "Dismiss all" }));

    await waitFor(() => expect(writes()).toEqual([[DISMISS, "POST", { all: true }]]));
    expect(confirm.mock.calls[1][0]).toContain("120");
  });

  it("loads the list again after a dismiss", async () => {
    render(<UnmappedVersions />, { wrapper });
    await screen.findByRole("row", { name: "Fuel (demo)" });
    const before = pageRequests().length;

    fireEvent.click(screen.getByRole("button", { name: "Dismiss demo on Fuel" }));

    await waitFor(() => expect(pageRequests().length).toBeGreaterThan(before));
  });

  it("waits while a write is in flight", async () => {
    let finish: () => void = () => {};
    serve(
      () =>
        new Promise<never>((resolve) => {
          finish = () => resolve(undefined as never);
        })
    );
    render(<UnmappedVersions />, { wrapper });
    await screen.findByRole("row", { name: "Fuel (demo)" });

    fireEvent.click(screen.getByRole("button", { name: "Dismiss demo on Fuel" }));

    const map = screen.getByRole("button", { name: "Map live on One" }) as HTMLButtonElement;
    await waitFor(() => expect(map.disabled).toBe(true));
    fireEvent.click(map);
    expect(writes()).toHaveLength(1);
    finish();
    await waitFor(() => expect(map.disabled).toBe(false));
  });

  it("says why a write was refused", async () => {
    serve(() => Promise.reject(new ConflictError("Version 'live' is mapped; unmap it first")));
    render(<UnmappedVersions />, { wrapper });
    await screen.findByRole("row", { name: "One (live)" });

    fireEvent.click(screen.getByRole("button", { name: "Dismiss live on One" }));

    expect((await screen.findByRole("alert")).textContent).toContain(
      "Version 'live' is mapped; unmap it first"
    );
  });
});
