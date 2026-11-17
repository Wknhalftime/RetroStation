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

// Phase 3b PR 3 (SPEC S3, Q19, D24): a work a merge took redirects. The server answers a
// merged-away id with the merged work's detail, under that work's own id; the page then
// replaces its URL with the merged work's, so Back skips the old address.

const mockedApiFetch = vi.mocked(apiFetch);
const FILE = "11111111-1111-4111-8111-111111111111";

const MERGED = {
  id: "w-kept",
  title: "Always",
  artist_id: "a-bj",
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
};

function serve() {
  mockedApiFetch.mockImplementation((url: string) => {
    if (url === "/api/v1/library/works/w-gone" || url === "/api/v1/library/works/w-kept") {
      return Promise.resolve(MERGED);
    }
    if (url === "/api/v1/library/artists/a-bj") {
      return Promise.resolve({ id: "a-bj", name: "Bon Jovi" });
    }
    return Promise.reject(new ApiError(404, `unexpected ${url}`));
  });
}

/** Where the page is, and how it got there. */
function Where() {
  const location = useLocation();
  const how = useNavigationType();
  return <p data-testid="where">{`${how} ${location.pathname}`}</p>;
}

function renderAt(path: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route
            path="/library/artists/:artist_id/works/:work_id"
            element={
              <>
                <AssociatedWorks />
                <Where />
              </>
            }
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

describe("AssociatedWorks, a merged-away work", () => {
  beforeEach(() => {
    mockedApiFetch.mockReset();
  });

  it("replaces the URL with the merged work's", async () => {
    serve();
    renderAt("/library/artists/a-bj/works/w-gone");

    expect(await screen.findByText("REPLACE /library/artists/a-bj/works/w-kept")).toBeTruthy();
    expect(await screen.findByRole("heading", { name: "Always" })).toBeTruthy();
  });

  it("stays on a work that was never merged", async () => {
    serve();
    renderAt("/library/artists/a-bj/works/w-kept");

    expect(await screen.findByRole("heading", { name: "Always" })).toBeTruthy();
    expect(screen.getByTestId("where").textContent).toBe("POP /library/artists/a-bj/works/w-kept");
  });
});
