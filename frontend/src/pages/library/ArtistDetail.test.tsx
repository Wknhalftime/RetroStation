// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";

vi.mock("@/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/api/client")>();
  return { ...actual, apiFetch: vi.fn() };
});

import { ApiError, ConflictError, apiFetch } from "@/api/client";
import { ArtistDetail } from "./ArtistDetail";

// Phase 2a S11 (M33): Merge on the artist page picks another artist by name or by MBID, asks
// to confirm, and opens the primary; a refusal says why and stays. A merged artist's page
// opens its primary with a banner. Unmerge, once confirmed, deletes the member's one edge.

const mockedApiFetch = vi.mocked(apiFetch);
const PRINCE_MBID = "070d193a-845c-479f-980e-bef15710653e";
const NPG_MBID = "0a000000-0000-4000-8000-0000000000d1";
const NPG_NAME = "Prince & The New Power Generation";
const MERGE_URL = "/api/v1/library/artists/a-npg/merge";

function detail(id: string, name: string, mbid: string) {
  return {
    id,
    name,
    sort_name: name,
    disambiguation: null,
    mbid,
    origin: "musicbrainz",
    works: [],
    merged_into: null as string | null,
    members: [] as { id: string; name: string; mbid: string | null }[],
  };
}

const PRINCE = detail("a-prince", "Prince", PRINCE_MBID);
const NPG = detail("a-npg", NPG_NAME, NPG_MBID);
const MERGED = {
  "a-npg": { ...NPG, merged_into: "a-prince" },
  "a-prince": { ...PRINCE, members: [{ id: "a-npg", name: NPG_NAME, mbid: NPG_MBID }] },
};
const APART = { "a-npg": NPG, "a-prince": PRINCE };

function summary(artist: typeof PRINCE) {
  return {
    id: artist.id,
    name: artist.name,
    sort_name: artist.sort_name,
    disambiguation: null,
    work_count: 0,
    file_count: 0,
    mbid: artist.mbid,
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
      return Promise.resolve({ items: [summary(NPG), summary(PRINCE)], total: 2 });
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
  fireEvent.click(await screen.findByRole("button", { name: /^Prince(?! &)/ }));
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
    renderAt("/library/artists/a-npg");

    fireEvent.click(await screen.findByRole("button", { name: /^Merge/ }));
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "prince" } });
    await waitFor(() => expect(searches().some((p) => p.get("search") === "prince")).toBe(true));
    await screen.findByRole("button", { name: /^Prince(?! &)/ });
    // This artist is never offered as its own primary.
    expect(screen.queryByRole("button", { name: new RegExp(`^${NPG_NAME}`) })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /^Prince(?! &)/ }));

    expect(confirm).toHaveBeenCalledTimes(1);
    expect(confirm.mock.calls[0][0]).toContain(NPG_NAME);
    await waitFor(() => expect(writes("POST")).toHaveLength(1));
    expect(writes("POST")[0].url).toBe(MERGE_URL);
    expect(writes("POST")[0].body).toMatchObject({ primary_id: "a-prince" });
    await waitFor(() => expect(where()).toBe("/library/artists/a-prince"));
  });

  it("finds the primary by MBID", async () => {
    serve();
    vi.spyOn(window, "confirm").mockReturnValue(true);
    renderAt("/library/artists/a-npg");

    await pick(PRINCE_MBID);

    expect(searches().some((p) => p.get("mbid") === PRINCE_MBID)).toBe(true);
    await waitFor(() => expect(writes("POST")).toHaveLength(1));
    expect(writes("POST")[0].body).toMatchObject({ primary_id: "a-prince" });
  });

  it("sends nothing when the merge isn't confirmed", async () => {
    serve();
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    renderAt("/library/artists/a-npg");

    await pick("prince");

    expect(confirm).toHaveBeenCalledTimes(1);
    expect(writes("POST")).toEqual([]);
    expect(where()).toBe("/library/artists/a-npg");
  });

  it("says why a merge was refused, and stays on the page", async () => {
    serve(new ConflictError("Merging these would make a cycle"));
    vi.spyOn(window, "confirm").mockReturnValue(true);
    renderAt("/library/artists/a-npg");

    await pick("prince");

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("Merging these would make a cycle");
    expect(where()).toBe("/library/artists/a-npg");
  });

  it("opens a merged artist's primary, with a banner naming the merged artist", async () => {
    details = { ...MERGED };
    serve();
    renderAt("/library/artists/a-npg");

    await waitFor(() => expect(where()).toBe("/library/artists/a-prince"));
    await screen.findByRole("heading", { name: "Prince" });
    const banner = await screen.findByRole("status");
    expect(banner.textContent).toContain(NPG_NAME);
    expect(banner.textContent).toContain("merged into");
  });

  it("unmerges a member once confirmed, and the page shows it gone", async () => {
    details = { ...MERGED };
    serve();
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    renderAt("/library/artists/a-prince");

    const unmerge = await screen.findByRole("button", { name: new RegExp(`Unmerge.*${NPG_NAME}`) });
    fireEvent.click(unmerge);
    expect(confirm).toHaveBeenCalledTimes(1);
    expect(writes("DELETE")).toEqual([]);

    confirm.mockReturnValue(true);
    fireEvent.click(unmerge);

    await waitFor(() => expect(writes("DELETE")).toEqual([{ url: MERGE_URL, body: undefined }]));
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: new RegExp(`Unmerge.*${NPG_NAME}`) })).toBeNull()
    );
    expect(where()).toBe("/library/artists/a-prince");
  });
});
