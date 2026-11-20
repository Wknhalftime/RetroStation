// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation, useNavigationType } from "react-router-dom";

vi.mock("@/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/api/client")>();
  return { ...actual, apiFetch: vi.fn() };
});

import { ApiError, apiFetch } from "@/api/client";
import { AssociatedWorks } from "./AssociatedWorks";

// Phase 3b PR 5a (SPEC S8, D41 b): the work page has its own address, /library/works/:work_id,
// and the artist's .../works/:work_id still opens it. Its back link goes to the artist the
// address names, else the Library. A merged-away work redirects at either address (3b S3).

const mockedApiFetch = vi.mocked(apiFetch);
const FILE = "11111111-1111-4111-8111-111111111111";

const KEPT = {
  id: "w-kept",
  title: "Always",
  artist_id: "a-bj",
  artists: [{ id: "a-bj", name: "Bon Jovi" }],
  recordings: [
    {
      id: "rec-1",
      title: "Always",
      version_type: "original",
      duration_ms: 240_000,
      files: [
        {
          id: FILE,
          file_path: "D:\\Music\\always.flac",
          format: "flac",
          bitrate: 900,
          duration_ms: 240_000,
          track_title: "Always",
          release_title: null,
          enrichment_status: "enriched",
          file_status: "present",
        },
      ],
    },
  ],
  song_master: null,
  format_overrides: [],
  performances: [],
  acts: [],
};

function serve() {
  mockedApiFetch.mockImplementation((url: string) => {
    if (url === "/api/v1/library/works/w-gone" || url === "/api/v1/library/works/w-kept") {
      return Promise.resolve(KEPT);
    }
    if (url === "/api/v1/library/artists/a-bj") {
      return Promise.resolve({ id: "a-bj", name: "Bon Jovi" });
    }
    return Promise.reject(new ApiError(404, `unexpected ${url}`));
  });
}

function Where() {
  const location = useLocation();
  const how = useNavigationType();
  return <p data-testid="where">{`${how} ${location.pathname}`}</p>;
}

function renderAt(path: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const page = (
    <>
      <AssociatedWorks />
      <Where />
    </>
  );
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/library/works/:work_id" element={page} />
          <Route path="/library/artists/:artist_id/works/:work_id" element={page} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

const artistReads = () =>
  mockedApiFetch.mock.calls.filter(([url]) => url.startsWith("/api/v1/library/artists"));

describe("AssociatedWorks, the work page's addresses", () => {
  beforeEach(() => {
    mockedApiFetch.mockReset();
  });

  it("opens at its own address, its back link to the Library", async () => {
    serve();
    renderAt("/library/works/w-kept");

    expect(await screen.findByRole("heading", { name: "Always", level: 1 })).toBeTruthy();
    const back = screen.getByRole("link", { name: "Library" });
    expect(back.getAttribute("href")).toBe("/library");
    expect(artistReads()).toEqual([]);
  });

  it("still opens at the artist's address, its back link to that artist", async () => {
    serve();
    renderAt("/library/artists/a-bj/works/w-kept");

    expect(await screen.findByRole("heading", { name: "Always", level: 1 })).toBeTruthy();
    const back = await screen.findByRole("link", { name: "Bon Jovi" });
    expect(back.getAttribute("href")).toBe("/library/artists/a-bj");
  });

  it("replaces a merged-away work's own address with the merged work's", async () => {
    serve();
    renderAt("/library/works/w-gone");

    expect(await screen.findByText("REPLACE /library/works/w-kept")).toBeTruthy();
    expect(await screen.findByRole("heading", { name: "Always", level: 1 })).toBeTruthy();
  });
});
