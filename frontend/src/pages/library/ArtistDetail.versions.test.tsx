// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";

vi.mock("@/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/api/client")>();
  return { ...actual, apiFetch: vi.fn() };
});

import { ApiError, apiFetch } from "@/api/client";
import { ArtistDetail } from "./ArtistDetail";

// Versions phase 2b PR 3, SPEC S7 and D5 (C62): the artist page says how many new versions the
// artist's group has, and each work how many of its own; Dismiss all, once confirmed, dismisses
// every unmapped version of the group's works. The fields are optional (2a's locked
// ArtistDetail.test.tsx serves none), so without them the page shows neither.

const mockedApiFetch = vi.mocked(apiFetch);
const DISMISS = "/api/v1/library/versions/dismiss";

function work(id: string, title: string, extra: Record<string, unknown> = {}) {
  return { id, title, recording_count: 2, has_master: true, version_types: [], ...extra };
}

function detail(extra: Record<string, unknown> = {}) {
  return {
    id: "a-prince",
    name: "Prince",
    sort_name: "Prince",
    disambiguation: null,
    works: [
      work("w-purple", "Purple Rain", { new_version_count: 2 }),
      work("w-kiss", "Kiss", { new_version_count: 0 }),
      work("w-blue", "Computer Blue"),
    ],
    merged_into: null,
    members: [],
    ...extra,
  };
}

let served: Record<string, unknown>;

function writes(): [string, string, unknown][] {
  return mockedApiFetch.mock.calls
    .filter(([, init]) => (init as RequestInit | undefined)?.method !== undefined)
    .map(([url, init]) => {
      const options = init as RequestInit;
      return [url, options.method ?? "GET", options.body ? JSON.parse(String(options.body)) : null];
    });
}

function detailRequests(): number {
  return mockedApiFetch.mock.calls.filter(([url]) => url === "/api/v1/library/artists/a-prince")
    .length;
}

function renderPage() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/library/artists/a-prince"]}>
        <Routes>
          <Route path="/library/artists/:artist_id" element={<ArtistDetail />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

beforeEach(() => {
  mockedApiFetch.mockReset();
  served = detail({ new_version_count: 3 });
  mockedApiFetch.mockImplementation((url: string, init?: RequestInit) => {
    if (init?.method === "POST" && url === DISMISS) return Promise.resolve({ dismissed: 3 });
    if (url === "/api/v1/library/artists/a-prince") return Promise.resolve(served);
    return Promise.reject(new ApiError(404, "not found"));
  });
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("ArtistDetail: new versions", () => {
  it("says how many new versions the group has, and each work its own", async () => {
    renderPage();

    await screen.findByRole("heading", { name: "Prince" });
    await waitFor(() => expect(document.body.textContent).toContain("3 new versions"));
    expect(screen.getByRole("link", { name: /Purple Rain/ }).textContent).toContain("2 new");
    expect(screen.getByRole("link", { name: /Kiss/ }).textContent).not.toMatch(/new/);
    expect(screen.getByRole("link", { name: /Computer Blue/ }).textContent).not.toMatch(/new/);
  });

  it("asks before Dismiss all, then dismisses the group's versions and loads the page again", async () => {
    const confirm = vi
      .spyOn(window, "confirm")
      .mockReturnValueOnce(false)
      .mockReturnValueOnce(true);
    renderPage();
    const button = await screen.findByRole("button", { name: "Dismiss all" });
    const before = detailRequests();

    fireEvent.click(button);
    expect(confirm).toHaveBeenCalledTimes(1);
    expect(writes()).toEqual([]);
    fireEvent.click(button);

    await waitFor(() => expect(writes()).toEqual([[DISMISS, "POST", { artist_id: "a-prince" }]]));
    expect(confirm.mock.calls[1][0]).toContain("3");
    await waitFor(() => expect(detailRequests()).toBeGreaterThan(before));
  });

  it.each([
    ["without the field", detail()],
    ["with none", detail({ new_version_count: 0 })],
  ])("shows no count and no Dismiss all %s", async (_, body) => {
    served = body;
    renderPage();

    expect(await screen.findByRole("heading", { name: "Prince" })).toBeTruthy();
    expect(screen.queryByText(/new version/)).toBeNull();
    expect(screen.queryByRole("button", { name: "Dismiss all" })).toBeNull();
  });
});
