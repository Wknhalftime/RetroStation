// @vitest-environment jsdom
/**
 * MatcherBrowser component tests.
 *
 * These tests assert the observable contract of the queue browser via
 * mocked `apiFetch`. They deliberately scope to rendering + a single
 * user interaction per test to avoid flakey multi-page React Query
 * timing races — deeper flows (pagination state across multiple
 * fetches, clamp effects after total shrinks) are covered implicitly
 * by the hook-level tests and the comments describing the invariants
 * in MatcherBrowser.tsx.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, within, act } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { notifyManager } from "@tanstack/query-core";
import type { ReactNode } from "react";
import type { QueueArtist } from "@/lib/schemas/matcher";

vi.mock("@/api/client", () => ({
  apiFetch: vi.fn(),
}));

import { apiFetch } from "@/api/client";
import { MatcherBrowser } from "./MatcherBrowser";

function makeClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
        // Disable focus-triggered refetches so jsdom focus events (fired
        // by act() / waitFor internally) don't cause a spurious re-fetch
        // of the previous page's query during pagination tests.
        refetchOnWindowFocus: false,
        // Prevent immediate stale-triggered background refetches.  With the
        // default staleTime of 0 every resolved fetch is immediately "stale",
        // and some React Query / React 19 interactions can cause a spurious
        // remount-triggered re-fetch of the previous-page query.
        staleTime: Infinity,
      },
      mutations: { retry: false },
    },
  });
}

function wrapperFor(qc: QueryClient) {
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
}

const mockedApiFetch = vi.mocked(apiFetch);

beforeEach(() => {
  mockedApiFetch.mockReset();
  // TanStack Query v5 normally uses setTimeout(0) to batch+schedule store
  // notifications.  In tests we need those notifications to fire *within* the
  // React act() window so that React commits the resulting re-renders before
  // waitFor starts polling.  Using a synchronous scheduler means the notify
  // callbacks run immediately inside the batch flush, which happens in the
  // same microtask chain as the resolved fetch — all inside await act().
  notifyManager.setScheduler((cb) => cb());
});

afterEach(() => {
  // Restore the default macrotask scheduler so this test file doesn't bleed
  // into other test suites that rely on the real scheduling behaviour.
  notifyManager.setScheduler((cb) => setTimeout(cb, 0));
});

function makeArtist(id: string, name: string): QueueArtist {
  return {
    id,
    original_name: name,
    normalized_name: name.toLowerCase(),
    match_status: "needs_review",
    triage_bucket: "blocked",
    candidates: [],
    identities: [],
  };
}

describe("MatcherBrowser rendering", () => {
  it('shows "Resolution Center" heading and the queue count', async () => {
    mockedApiFetch.mockResolvedValue({
      items: [makeArtist("00000000-0000-0000-0000-00000000000a", "Alpha")],
      total: 1,
    });
    render(<MatcherBrowser />, { wrapper: wrapperFor(makeClient()) });
    expect(screen.getByRole("heading", { name: /Resolution Center/i })).toBeDefined();
    await waitFor(() => {
      expect(screen.getByText(/Artists \(1\)/)).toBeDefined();
    });
  });

  it('shows "Queue is empty" only when the backend reports total === 0', async () => {
    // Regression: before the fix in PR #25 round 3, an empty page response
    // with total > 0 wrongly triggered the "queue empty" UI. The condition
    // is now gated on `total === 0`.
    mockedApiFetch.mockResolvedValue({ items: [], total: 0 });
    render(<MatcherBrowser />, { wrapper: wrapperFor(makeClient()) });
    await waitFor(() => {
      expect(screen.getByText(/Queue is empty/i)).toBeDefined();
    });
  });

  it('does NOT show "Queue is empty" when total > 0 even if items is empty (off-range page simulation)', async () => {
    mockedApiFetch.mockResolvedValue({ items: [], total: 10 });
    render(<MatcherBrowser />, { wrapper: wrapperFor(makeClient()) });
    // Wait for the query to resolve via a positive signal (the first
    // apiFetch call) rather than a fixed setTimeout — otherwise a fast
    // test run could assert before the query even fires and pass by
    // accident. Once the fetch has been made and React has committed
    // the render, the empty-state check is deterministic.
    await waitFor(() => {
      expect(mockedApiFetch).toHaveBeenCalled();
    });
    expect(screen.queryByText(/Queue is empty/i)).toBeNull();
  });
});

describe("MatcherBrowser — pagination across loading transitions", () => {
  it("keeps the page indicator on 'Page 2 of 4' across the Next click and fetch (regression: bug-1)", async () => {
    const page1Items = [makeArtist("00000000-0000-0000-0000-000000000001", "Alpha")];
    const page2Items = [makeArtist("00000000-0000-0000-0000-000000000002", "Bravo")];

    let resolvePage2: (val: { items: QueueArtist[]; total: number }) => void = () => {};
    const page2Promise = new Promise<{ items: QueueArtist[]; total: number }>((r) => {
      resolvePage2 = r;
    });

    mockedApiFetch.mockImplementation(async (url) => {
      const u = typeof url === "string" ? url : "";
      if (u.includes("/api/v1/matching/queue")) {
        if (u.includes("offset=0")) return { items: page1Items, total: 100 };
        if (u.includes("offset=25")) return page2Promise;
      }
      return {};
    });

    render(<MatcherBrowser />, { wrapper: wrapperFor(makeClient()) });

    await screen.findByText(/Page 1 of 4/);

    fireEvent.click(screen.getByRole("button", { name: /Next/i }));

    await screen.findByText(/Page 2 of 4/);

    resolvePage2({ items: page2Items, total: 100 });
    await screen.findByText("Bravo");
    expect(screen.queryByText("Alpha")).toBeNull();
    expect(screen.getByText(/Page 2 of 4/)).toBeDefined();
  });
});

describe("MatcherBrowser — MB search target-artist capture", () => {
  it("captures the queue artist ID at MB-search-open time so a later selection change does not redirect the mutation", async () => {
    const artistA = makeArtist("00000000-0000-0000-0000-00000000000a", "Alpha");
    const artistB = makeArtist("00000000-0000-0000-0000-00000000000b", "Beta");
    mockedApiFetch.mockImplementation(async (url) => {
      const u = typeof url === "string" ? url : "";
      if (u.includes("/api/v1/matching/queue")) {
        return { items: [artistA, artistB], total: 2 };
      }
      if (u.includes("/api/v1/matching/mb-artists")) {
        return {
          items: [
            {
              id: "mbid-target",
              name: "Target",
              score: 100,
              disambiguation: "",
            },
          ],
        };
      }
      if (u.match(/\/api\/v1\/matching\/artists\/[^/]+\/resolve/)) {
        return { id: "x", match_status: "manual_matched" };
      }
      return {};
    });
    render(<MatcherBrowser />, { wrapper: wrapperFor(makeClient()) });

    // Wait for the sidebar list to render, then scope interactions to it.
    // The sidebar buttons contain the artist name PLUS a status badge, so
    // we match by substring rather than exact name.
    await screen.findByText(/Artists \(2\)/);
    const sidebar = screen.getByText(/Artists \(2\)/).closest("aside") as HTMLElement;
    const alphaSidebarButton = within(sidebar).getByRole("button", { name: /Alpha/i });
    const betaSidebarButton = within(sidebar).getByRole("button", { name: /Beta/i });

    fireEvent.click(alphaSidebarButton);
    fireEvent.click(await screen.findByRole("button", { name: /Search MusicBrainz/i }));

    // Change queue selection to Beta WHILE the slide-over is open.
    fireEvent.click(betaSidebarButton);

    // The slide-over's searchbox — scope to its aria-labelled aside so we
    // don't pick up the file-mode slide-over also present in the DOM.
    const mbAside = screen.getByRole("complementary", {
      name: /Search MusicBrainz artists/i,
    });
    const searchInput = mbAside.querySelector('input[type="search"]') as HTMLInputElement;
    fireEvent.change(searchInput, { target: { value: "target" } });

    // Click the MB result.
    const mbResultButton = await screen.findByRole("button", {
      name: /Target/i,
    });
    fireEvent.click(mbResultButton);

    // The resolve mutation must hit Alpha's URL, NOT Beta's.
    await waitFor(() => {
      const resolveCalls = mockedApiFetch.mock.calls.filter(
        ([u]) => typeof u === "string" && !!u.match(/\/api\/v1\/matching\/artists\/[^/]+\/resolve/)
      );
      expect(resolveCalls.length).toBeGreaterThan(0);
      const url = resolveCalls[0][0] as string;
      expect(url).toContain(artistA.id);
      expect(url).not.toContain(artistB.id);
    });
  });
});


describe("MatcherBrowser — library search target-artist capture", () => {
  it("captures the queue artist ID at library-search-open time so a later selection change does not redirect the mutation", async () => {
    const artistA = makeArtist("00000000-0000-0000-0000-00000000000a", "Alpha");
    const artistB = makeArtist("00000000-0000-0000-0000-00000000000b", "Beta");
    const localArtist = { id: "local-uuid-1", name: "Ultraspank", mbid: "mbid-ultra" };

    mockedApiFetch.mockImplementation(async (url) => {
      const u = typeof url === "string" ? url : "";
      if (u.includes("/api/v1/matching/queue")) {
        return { items: [artistA, artistB], total: 2 };
      }
      if (u.includes("/api/v1/library/artists")) {
        return { items: [localArtist], total: 1 };
      }
      if (u.match(/\/api\/v1\/matching\/artists\/[^/]+\/resolve/)) {
        return { id: "x", match_status: "manual_matched" };
      }
      return {};
    });
    render(<MatcherBrowser />, { wrapper: wrapperFor(makeClient()) });

    await screen.findByText(/Artists \(2\)/);
    const sidebar = screen.getByText(/Artists \(2\)/).closest("aside") as HTMLElement;
    const alphaSidebarButton = within(sidebar).getByRole("button", { name: /Alpha/i });
    const betaSidebarButton = within(sidebar).getByRole("button", { name: /Beta/i });

    fireEvent.click(alphaSidebarButton);
    fireEvent.click(await screen.findByRole("button", { name: /Search Library/i }));

    // Change queue selection to Beta WHILE the library slide-over is open.
    fireEvent.click(betaSidebarButton);

    const libraryAside = screen.getByRole("complementary", { name: /^Search artists$/i });
    const searchInput = libraryAside.querySelector('input[type="search"]') as HTMLInputElement;
    fireEvent.change(searchInput, { target: { value: "ultra" } });

    const libraryResultButton = await screen.findByRole("button", { name: /Ultraspank/i });
    fireEvent.click(libraryResultButton);

    // The resolve mutation must hit Alpha's URL, NOT Beta's.
    await waitFor(() => {
      const resolveCalls = mockedApiFetch.mock.calls.filter(
        ([u]) => typeof u === "string" && !!u.match(/\/api\/v1\/matching\/artists\/[^/]+\/resolve/)
      );
      expect(resolveCalls.length).toBeGreaterThan(0);
      const url = resolveCalls[0][0] as string;
      expect(url).toContain(artistA.id);
      expect(url).not.toContain(artistB.id);
    });
  });
});

describe("MatcherBrowser — library search MBID fallback", () => {
  async function resolveViaLibrarySearch(
    localArtist: { id: string; name: string; mbid: string | null }
  ): Promise<{ url: string; body: Record<string, unknown> }> {
    const artist = makeArtist("00000000-0000-0000-0000-00000000000a", "Alpha");

    mockedApiFetch.mockImplementation(async (url) => {
      const u = typeof url === "string" ? url : "";
      if (u.includes("/api/v1/matching/queue")) return { items: [artist], total: 1 };
      if (u.includes("/api/v1/library/artists")) return { items: [localArtist], total: 1 };
      if (u.match(/\/api\/v1\/matching\/artists\/[^/]+\/resolve/))
        return { id: "x", match_status: "manual_matched" };
      return {};
    });

    render(<MatcherBrowser />, { wrapper: wrapperFor(makeClient()) });
    await screen.findByText(/Artists \(1\)/);
    const sidebar = screen.getByText(/Artists \(1\)/).closest("aside") as HTMLElement;
    fireEvent.click(within(sidebar).getByRole("button", { name: /Alpha/i }));
    fireEvent.click(await screen.findByRole("button", { name: /Search Library/i }));

    const libraryAside = screen.getByRole("complementary", { name: /^Search artists$/i });
    const searchInput = libraryAside.querySelector('input[type="search"]') as HTMLInputElement;
    fireEvent.change(searchInput, { target: { value: "ultra" } });

    fireEvent.click(await screen.findByRole("button", { name: /Ultraspank/i }));

    let resolveCall: { url: string; body: Record<string, unknown> } | null = null;
    await waitFor(() => {
      const calls = mockedApiFetch.mock.calls.filter(
        ([u]) => typeof u === "string" && !!u.match(/\/api\/v1\/matching\/artists\/[^/]+\/resolve/)
      );
      expect(calls.length).toBeGreaterThan(0);
      const [url, opts] = calls[0] as [string, { method: string; body: string }];
      resolveCall = { url, body: JSON.parse(opts.body) as Record<string, unknown> };
    });
    return resolveCall!;
  }

  it("uses artist.mbid as target_artist_id when mbid is a non-empty string", async () => {
    const { body } = await resolveViaLibrarySearch({
      id: "local-uuid-1",
      name: "Ultraspank",
      mbid: "mbid-ultra",
    });
    expect(body["target_artist_id"]).toBe("mbid-ultra");
  });

  it("falls back to artist.id when mbid is null", async () => {
    mockedApiFetch.mockReset(); // clear calls from previous test
    const { body } = await resolveViaLibrarySearch({
      id: "local-uuid-1",
      name: "Ultraspank",
      mbid: null,
    });
    expect(body["target_artist_id"]).toBe("local-uuid-1");
  });

  it("falls back to artist.id when mbid is an empty string", async () => {
    mockedApiFetch.mockReset();
    const { body } = await resolveViaLibrarySearch({
      id: "local-uuid-1",
      name: "Ultraspank",
      mbid: "",
    });
    expect(body["target_artist_id"]).toBe("local-uuid-1");
  });

  it("uses the trimmed mbid when mbid contains surrounding whitespace", async () => {
    // Guards the review finding: checking trim() but passing the untrimmed
    // original would persist a whitespace-padded id to the backend.
    mockedApiFetch.mockReset();
    const { body } = await resolveViaLibrarySearch({
      id: "local-uuid-1",
      name: "Ultraspank",
      mbid: "  mbid-ultra  ",
    });
    expect(body["target_artist_id"]).toBe("mbid-ultra");
  });
});

describe("MatcherBrowser — pagination", () => {
  it("navigating to the next page does not reset to page 1 while the new page is loading", async () => {
    const page1Artists = Array.from({ length: 25 }, (_, i) =>
      makeArtist(
        `00000000-0000-0000-0000-0000000000${String(i).padStart(2, "0")}`,
        `Artist ${i + 1}`
      )
    );
    const page2Artists = Array.from({ length: 25 }, (_, i) =>
      makeArtist(
        `00000000-0000-0000-0000-0000000001${String(i).padStart(2, "0")}`,
        `Artist ${i + 26}`
      )
    );

    // Both pages resolve immediately — Promise.resolve() still goes through a
    // microtask tick, which is enough to expose the original bug (the clamping
    // useEffect would fire while data was transiently undefined, see totalPages
    // drop to 1, and reset page back to 1 before the data arrived).
    mockedApiFetch.mockImplementation((url) => {
      const u = typeof url === "string" ? url : "";
      if (u.includes("offset=0"))  return Promise.resolve({ items: page1Artists, total: 50 });
      if (u.includes("offset=25")) return Promise.resolve({ items: page2Artists, total: 50 });
      return Promise.resolve({ items: [], total: 0 });
    });

    render(<MatcherBrowser />, { wrapper: wrapperFor(makeClient()) });

    // Wait until page 1 is fully rendered.
    await waitFor(() => {
      expect(screen.getByText(/Page 1 of 2/)).toBeDefined();
    });

    // Click Next — page state becomes 2. The bug: without the fix the clamping
    // useEffect fires while data is transiently undefined (totalPages drops to
    // 1) and resets page back to 1.
    //
    // Use async act() so that after the click React 19 flushes all pending
    // work: the setPage(2) render, the observer.setOptions effect, the fetch
    // microtask, and the subsequent TanStack Query queueMicrotask notification
    // (queueMicrotask because we override the scheduler in beforeEach).
    // Plain fireEvent.click uses only synchronous act(), which ends before
    // those microtasks can run, leaving the notification unprocessed.
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /Next/i }));
    });

    // By the time await act() resolves, the synchronous TQ scheduler has fired
    // all store notifications and React has committed the page-2 render.
    // Use waitFor as a safety net for any remaining async React work.
    await waitFor(() => {
      expect(screen.getByText("Artist 26")).toBeDefined();
    });

    // Secondary assertions: page-1 artists must be gone once page 2 loaded.
    expect(screen.queryByText("Artist 1")).toBeNull();
    expect(screen.getByText(/Page 2 of 2/)).toBeDefined();
  });
});
