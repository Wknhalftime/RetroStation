// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";

vi.mock("@/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/api/client")>();
  return { ...actual, apiFetch: vi.fn() };
});

import { apiFetch } from "@/api/client";
import { AssociatedWorks } from "./AssociatedWorks";

// A song master's score is a points total (release status + release type +
// format, 0-190), not a fraction: a score of 10 used to read "Score: 1000%".

const mockedApiFetch = vi.mocked(apiFetch);
const FILE = "11111111-1111-4111-8111-111111111111";
const WORK_URL = "/api/v1/library/works/w-fos";

function workWithScore(score: number | null) {
  return {
    id: "w-fos",
    title: "I Ran (So Far Away)",
    artist_id: "a-fos",
    recordings: [],
    song_master: {
      id: "33333333-3333-4333-8333-333333333333",
      preferred_file_id: FILE,
      selection_method: "auto",
      score,
      updated_at: "2026-11-07T12:00:00Z",
    },
    format_overrides: [],
  };
}

function renderWithScore(score: number | null) {
  mockedApiFetch.mockImplementation((url: string) =>
    Promise.resolve(
      url === WORK_URL ? workWithScore(score) : { id: "a-fos", name: "A Flock of Seagulls" }
    )
  );
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/library/artists/a-fos/works/w-fos"]}>
        <Routes>
          <Route path="/library/artists/:artist_id/works/:work_id" element={<AssociatedWorks />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

async function bannerText() {
  return (await screen.findByText(/Selection: auto/)).textContent ?? "";
}

describe("AssociatedWorks, the master banner's score", () => {
  beforeEach(() => mockedApiFetch.mockReset());

  it("shows a FLAC-only score as 10 points, not 1000%", async () => {
    renderWithScore(10);

    const text = await bannerText();
    expect(text).toContain("Score: 10 points");
    expect(text).not.toContain("%");
  });

  it("shows the top score as it is", async () => {
    renderWithScore(190);

    expect(await bannerText()).toContain("Score: 190 points");
  });

  it("leaves the score out when the master has none", async () => {
    renderWithScore(null);

    expect(await bannerText()).not.toContain("Score");
  });
});
