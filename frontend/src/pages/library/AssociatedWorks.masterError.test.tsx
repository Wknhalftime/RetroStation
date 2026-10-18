// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";

vi.mock("@/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/api/client")>();
  return { ...actual, apiFetch: vi.fn() };
});

import { ApiError, ValidationError, apiFetch } from "@/api/client";
import { AssociatedWorks } from "./AssociatedWorks";

// A failed crown or "Revert to Auto" used to leave nothing on screen.

const mockedApiFetch = vi.mocked(apiFetch);
const FILE = "11111111-1111-4111-8111-111111111111";
const WORK_URL = "/api/v1/library/works/w-ss";

const WORK = {
  id: "w-ss",
  title: "Bother",
  artist_id: "a-ss",
  recordings: [
    {
      id: "rec-1",
      title: "Bother",
      version_type: "original",
      duration_ms: 240_000,
      files: [
        {
          id: FILE,
          file_path: "D:\\Music\\promo.flac",
          format: "flac",
          bitrate: 900,
          duration_ms: 240_000,
          track_title: "Bother",
          release_title: null,
          enrichment_status: "enriched",
          file_status: "present",
        },
      ],
    },
  ],
  song_master: {
    id: "33333333-3333-4333-8333-333333333333",
    preferred_file_id: FILE,
    selection_method: "manual",
    score: null,
    updated_at: "2026-10-18T12:00:00Z",
  },
  format_overrides: [],
};

function serve(failWith: Error) {
  mockedApiFetch.mockImplementation((url: string, options?: RequestInit) => {
    if (options?.method === "PUT" || options?.method === "DELETE") {
      return Promise.reject(failWith);
    }
    if (url === WORK_URL) return Promise.resolve(WORK);
    return Promise.resolve({ id: "a-ss", name: "Stone Sour" });
  });
}

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/library/artists/a-ss/works/w-ss"]}>
        <Routes>
          <Route path="/library/artists/:artist_id/works/:work_id" element={<AssociatedWorks />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

describe("AssociatedWorks, a master change the server refuses", () => {
  beforeEach(() => mockedApiFetch.mockReset());

  it("shows why the crown failed", async () => {
    serve(new ApiError(404, "Work w-ss not found"));
    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: "Current master file" }));

    expect((await screen.findByRole("alert")).textContent).toContain("Work w-ss not found");
  });

  it("shows a refusal's own reason, not 'Validation error'", async () => {
    const refused = new ValidationError([]);
    Object.defineProperty(refused, "detail", { value: `File ${FILE} is missing from disk` });
    serve(refused);
    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: "Current master file" }));

    expect((await screen.findByRole("alert")).textContent).toContain(
      `File ${FILE} is missing from disk`
    );
  });

  it("shows why Revert to Auto failed", async () => {
    serve(new ApiError(404, "No master found for work w-ss"));
    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: /Revert to Auto/ }));

    expect((await screen.findByRole("alert")).textContent).toContain(
      "No master found for work w-ss"
    );
  });
});
