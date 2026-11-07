// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Link, MemoryRouter, Route, Routes, useLocation } from "react-router-dom";

vi.mock("@/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/api/client")>();
  return { ...actual, apiFetch: vi.fn() };
});

import { ApiError, ConflictError, apiFetch } from "@/api/client";
import { ArtistDetail } from "./ArtistDetail";

// Phase 2a S11 (M33): Merge on the artist page picks another artist by name or by MBID, asks
// to confirm, and opens the primary; a refusal says why and stays. A merged artist's page
// opens its primary with a banner. Unmerge, once confirmed, deletes the member's one edge, and
// the member's own page opens again.

const mockedApiFetch = vi.mocked(apiFetch);
const PRINCE_MBID = "070d193a-845c-479f-980e-bef15710653e";
const REVOLUTION_MBID = "0a000000-0000-4000-8000-0000000000d1";
const MERGE_URL = "/api/v1/library/artists/a-rev/merge";

type Member = { id: string; name: string; mbid: string | null; primary_id: string };

// The fields GET /api/v1/library/artists/{id} sends.
function detail(id: string, name: string) {
  return {
    id,
    name,
    sort_name: name,
    disambiguation: null,
    works: [],
    merged_into: null as string | null,
    members: [] as Member[],
  };
}

const PRINCE = detail("a-prince", "Prince");
const REVOLUTION = detail("a-rev", "The Revolution");
const MERGED = {
  "a-rev": { ...REVOLUTION, merged_into: "a-prince" },
  "a-prince": {
    ...PRINCE,
    members: [
      { id: "a-rev", name: "The Revolution", mbid: REVOLUTION_MBID, primary_id: "a-prince" },
    ],
  },
};
const APART = { "a-rev": REVOLUTION, "a-prince": PRINCE };

function summary(id: string, name: string, mbid: string) {
  return {
    id,
    name,
    sort_name: name,
    disambiguation: null,
    work_count: 0,
    file_count: 0,
    mbid,
    origin: "musicbrainz",
  };
}

// What GET /api/v1/library/artists/{id} answers now; a merge or unmerge changes it.
let details: Record<string, unknown> = {};

function serve(mergeFails?: Error) {
  mockedApiFetch.mockImplementation((url: string, options?: RequestInit) => {
    const method = options?.method ?? "GET";
    if (method === "POST" && url === MERGE_URL) {
      if (mergeFails) return Promise.reject(mergeFails);
      details = { ...MERGED };
      return Promise.resolve(undefined);
    }
    if (method === "DELETE" && url === MERGE_URL) {
      details = { ...APART };
      return Promise.resolve(undefined);
    }
    if (url.startsWith("/api/v1/library/artists?")) {
      return Promise.resolve({
        items: [
          summary("a-rev", "The Revolution", REVOLUTION_MBID),
          summary("a-prince", "Prince", PRINCE_MBID),
        ],
        total: 2,
      });
    }
    const id = url.replace("/api/v1/library/artists/", "");
    if (id in details) return Promise.resolve(details[id]);
    return Promise.reject(new ApiError(404, `Artist ${id} not found`));
  });
}

function Where() {
  return <p data-testid="where">{useLocation().pathname}</p>;
}

function renderAt(path: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <Link to="/library/artists/a-rev">Open The Revolution</Link>
        <Routes>
          <Route
            path="/library/artists/:artist_id"
            element={
              <>
                <ArtistDetail />
                <Where />
              </>
            }
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

function where(): string {
  return screen.getByTestId("where").textContent ?? "";
}

function writes(method: "POST" | "DELETE") {
  return mockedApiFetch.mock.calls
    .filter(([, options]) => options?.method === method)
    .map(([url, options]) => ({
      url,
      body: options?.body === undefined ? undefined : JSON.parse(String(options.body)),
    }));
}

function searches(): URLSearchParams[] {
  return mockedApiFetch.mock.calls
    .map(([url]) => url)
    .filter((url) => url.startsWith("/api/v1/library/artists?"))
    .map((url) => new URLSearchParams(url.slice(url.indexOf("?") + 1)));
}

async function pick(query: string) {
  fireEvent.click(await screen.findByRole("button", { name: /^Merge/ }));
  fireEvent.change(screen.getByRole("searchbox"), { target: { value: query } });
  fireEvent.click(await screen.findByRole("button", { name: /^Prince/ }));
}

beforeEach(() => {
  mockedApiFetch.mockReset();
  details = { ...APART };
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("merge_and_unmerge", () => {
  it("merges this artist into one picked by name, once confirmed, and opens the primary", async () => {
    serve();
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    renderAt("/library/artists/a-rev");

    fireEvent.click(await screen.findByRole("button", { name: /^Merge/ }));
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "prince" } });
    await screen.findByRole("button", { name: /^Prince/ });
    const byName = searches().filter((p) => p.get("search") === "prince");
    expect(byName.length).toBeGreaterThan(0);
    expect(byName.every((p) => !p.has("mbid"))).toBe(true);
    // This artist is never offered as its own primary.
    expect(screen.queryByRole("button", { name: /^The Revolution/ })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /^Prince/ }));

    expect(confirm).toHaveBeenCalledTimes(1);
    expect(confirm.mock.calls[0][0]).toContain("The Revolution");
    expect(confirm.mock.calls[0][0]).toContain("Prince");
    await waitFor(() => expect(writes("POST")).toHaveLength(1));
    expect(writes("POST")[0].url).toBe(MERGE_URL);
    expect(writes("POST")[0].body).toMatchObject({ primary_id: "a-prince" });
    await waitFor(() => expect(where()).toBe("/library/artists/a-prince"));
  });

  it("finds the primary by MBID", async () => {
    serve();
    vi.spyOn(window, "confirm").mockReturnValue(true);
    renderAt("/library/artists/a-rev");

    await pick(PRINCE_MBID);

    const byMbid = searches().filter((p) => p.get("mbid") === PRINCE_MBID);
    expect(byMbid.length).toBeGreaterThan(0);
    expect(byMbid.every((p) => !p.has("search"))).toBe(true);
    await waitFor(() => expect(writes("POST")).toHaveLength(1));
    expect(writes("POST")[0].body).toMatchObject({ primary_id: "a-prince" });
  });

  it("sends nothing when the merge isn't confirmed", async () => {
    serve();
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    renderAt("/library/artists/a-rev");

    await pick("prince");

    expect(confirm).toHaveBeenCalledTimes(1);
    expect(writes("POST")).toEqual([]);
    expect(where()).toBe("/library/artists/a-rev");
  });

  it("says why a merge was refused, and stays on the page", async () => {
    serve(new ConflictError("Merging these would make a cycle"));
    vi.spyOn(window, "confirm").mockReturnValue(true);
    renderAt("/library/artists/a-rev");

    await pick("prince");

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("Merging these would make a cycle");
    expect(where()).toBe("/library/artists/a-rev");
  });

  it("opens a merged artist's primary, with a banner naming the merged artist", async () => {
    details = { ...MERGED };
    serve();
    renderAt("/library/artists/a-rev");

    await waitFor(() => expect(where()).toBe("/library/artists/a-prince"));
    await screen.findByRole("heading", { name: "Prince" });
    const banner = await screen.findByRole("status");
    expect(banner.textContent).toContain("The Revolution");
    expect(banner.textContent).toContain("merged into");
  });

  it("unmerges a member once confirmed, and the page shows it gone", async () => {
    details = { ...MERGED };
    serve();
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    renderAt("/library/artists/a-prince");

    const unmerge = await screen.findByRole("button", { name: /Unmerge.*The Revolution/ });
    fireEvent.click(unmerge);
    expect(confirm).toHaveBeenCalledTimes(1);
    expect(writes("DELETE")).toEqual([]);

    confirm.mockReturnValue(true);
    fireEvent.click(unmerge);

    await waitFor(() => expect(writes("DELETE")).toEqual([{ url: MERGE_URL, body: undefined }]));
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: /Unmerge.*The Revolution/ })).toBeNull()
    );
    expect(where()).toBe("/library/artists/a-prince");
  });

  it("opens an unmerged artist's own page, not the primary it was merged into", async () => {
    details = { ...MERGED };
    serve();
    vi.spyOn(window, "confirm").mockReturnValue(true);
    renderAt("/library/artists/a-rev");
    await waitFor(() => expect(where()).toBe("/library/artists/a-prince"));

    fireEvent.click(await screen.findByRole("button", { name: /Unmerge.*The Revolution/ }));
    await waitFor(() => expect(writes("DELETE")).toHaveLength(1));
    fireEvent.click(screen.getByRole("link", { name: "Open The Revolution" }));

    await screen.findByRole("heading", { name: "The Revolution" });
    expect(where()).toBe("/library/artists/a-rev");
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("names the members that go with a member when it is unmerged", async () => {
    details = {
      ...MERGED,
      "a-prince": {
        ...PRINCE,
        members: [
          { id: "a-rev", name: "The Revolution", mbid: REVOLUTION_MBID, primary_id: "a-prince" },
          { id: "a-wl", name: "Wendy & Lisa", mbid: null, primary_id: "a-rev" },
        ],
      },
    };
    serve();
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    renderAt("/library/artists/a-prince");

    fireEvent.click(await screen.findByRole("button", { name: /Unmerge.*The Revolution/ }));

    expect(confirm).toHaveBeenCalledTimes(1);
    expect(confirm.mock.calls[0][0]).toContain("The Revolution");
    expect(confirm.mock.calls[0][0]).toContain("Wendy & Lisa");
    await waitFor(() => expect(writes("DELETE")).toEqual([{ url: MERGE_URL, body: undefined }]));
  });
});
