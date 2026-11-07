// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";

vi.mock("@/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/api/client")>();
  return { ...actual, apiFetch: vi.fn() };
});

import { ApiError, apiFetch } from "@/api/client";
import { ArtistDetail } from "./ArtistDetail";

// Duplicate works are ticked on the artist page and merged in one go. The app
// picks the work that stays (most recordings, then list order); the server
// moves the files and re-picks the master.

const mockedApiFetch = vi.mocked(apiFetch);
const ARTIST_URL = "/api/v1/library/artists/a-fos";
const mergeUrl = (target: string) => `/api/v1/library/works/${target}/merge`;

const summary = (id: string, title: string, recording_count = 1) => ({
  id,
  title,
  recording_count,
  has_master: true,
  version_types: [],
});

const ARTIST = {
  id: "a-fos",
  name: "A Flock of Seagulls",
  sort_name: "Flock of Seagulls, A",
  disambiguation: null,
  mbid: null,
  origin: "local",
  works: [
    summary("w-iran", "I Ran", 1),
    summary("w-iran-full", "I Ran (So Far Away)", 2),
    summary("w-space", "Space Age Love Song", 1),
    summary("w-space-2", "Space Age Love Song (Single)", 1),
  ],
};

function serve(merge?: () => Promise<unknown>) {
  mockedApiFetch.mockImplementation((url: string, options?: RequestInit) => {
    if (options?.method === "POST") {
      return merge
        ? merge()
        : Promise.resolve({
            merged_file_count: 1,
            deleted_work_count: 1,
            dropped_override_count: 0,
          });
    }
    if (url === ARTIST_URL) return Promise.resolve(ARTIST);
    return Promise.reject(new ApiError(404, `unexpected ${url}`));
  });
}

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/library/artists/a-fos"]}>
        <Routes>
          <Route path="/library/artists/:artist_id" element={<ArtistDetail />} />
          <Route path="/library/artists/:artist_id/works/:work_id" element={<p>work page</p>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

async function tick(title: string) {
  fireEvent.click(await screen.findByRole("checkbox", { name: `Select ${title}` }));
}

function mergeCalls() {
  return mockedApiFetch.mock.calls.filter(([, options]) => options?.method === "POST");
}

describe("ArtistDetail, merging ticked duplicate works", () => {
  // A block body: returning the mock would make vitest call it as a teardown.
  beforeEach(() => {
    mockedApiFetch.mockReset();
  });

  it("gives every work a checkbox", async () => {
    serve();
    renderPage();

    expect(await screen.findAllByRole("checkbox")).toHaveLength(4);
  });

  it("says how to merge before anything is ticked", async () => {
    serve();
    renderPage();

    expect(await screen.findByText(/Tick two or more works that are the same song/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: /^Merge/ })).toBeNull();
  });

  it("does not open the work when its box is ticked", async () => {
    serve();
    renderPage();

    await tick("I Ran");

    expect(screen.queryByText("work page")).toBeNull();
    expect(screen.getByRole("heading", { name: "A Flock of Seagulls" })).toBeTruthy();
  });

  it("offers Merge only once two works are ticked", async () => {
    serve();
    renderPage();

    await tick("I Ran");
    expect(screen.queryByRole("button", { name: /^Merge/ })).toBeNull();

    await tick("I Ran (So Far Away)");
    expect(screen.getByRole("button", { name: "Merge 2 works" })).toBeTruthy();
  });

  it("keeps the work with the most recordings and sends the rest", async () => {
    serve();
    renderPage();

    await tick("I Ran");
    await tick("I Ran (So Far Away)");
    fireEvent.click(screen.getByRole("button", { name: "Merge 2 works" }));

    const keepSentence = /^Keep “I Ran \(So Far Away\)” and fold 1 other work into it\?/;
    expect(
      screen.getByText((_, el) => el?.tagName === "SPAN" && keepSentence.test(el.textContent ?? ""))
    ).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Confirm merge" }));

    await screen.findByRole("status");
    expect(mergeCalls()).toHaveLength(1);
    const [url, options] = mergeCalls()[0];
    expect(url).toBe(mergeUrl("w-iran-full"));
    expect(JSON.parse(String(options?.body))).toEqual({ source_work_ids: ["w-iran"] });
  });

  it("keeps the first in the list when recordings tie", async () => {
    serve();
    renderPage();

    await tick("Space Age Love Song (Single)");
    await tick("Space Age Love Song");
    fireEvent.click(screen.getByRole("button", { name: "Merge 2 works" }));
    fireEvent.click(screen.getByRole("button", { name: "Confirm merge" }));

    await screen.findByRole("status");
    const [url, options] = mergeCalls()[0];
    expect(url).toBe(mergeUrl("w-space"));
    expect(JSON.parse(String(options?.body))).toEqual({ source_work_ids: ["w-space-2"] });
  });

  it("sends nothing when the merge is cancelled", async () => {
    serve();
    renderPage();

    await tick("I Ran");
    await tick("I Ran (So Far Away)");
    fireEvent.click(screen.getByRole("button", { name: "Merge 2 works" }));
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    expect(mergeCalls()).toHaveLength(0);
    expect(screen.getByRole("button", { name: "Merge 2 works" })).toBeTruthy();
  });

  it("says what was merged and clears the ticks", async () => {
    serve();
    renderPage();

    await tick("I Ran");
    await tick("I Ran (So Far Away)");
    fireEvent.click(screen.getByRole("button", { name: "Merge 2 works" }));
    fireEvent.click(screen.getByRole("button", { name: "Confirm merge" }));

    expect((await screen.findByRole("status")).textContent).toContain(
      "Merged 1 work into “I Ran (So Far Away)”: 1 file moved."
    );
    const boxes = screen.getAllByRole("checkbox") as HTMLInputElement[];
    expect(boxes.some((b) => b.checked)).toBe(false);
  });

  it("shows why the server refused", async () => {
    serve(() => Promise.reject(new ApiError(404, "Work w-iran-full not found")));
    renderPage();

    await tick("I Ran");
    await tick("I Ran (So Far Away)");
    fireEvent.click(screen.getByRole("button", { name: "Merge 2 works" }));
    fireEvent.click(screen.getByRole("button", { name: "Confirm merge" }));

    expect((await screen.findByRole("alert")).textContent).toContain("Work w-iran-full not found");
  });
});
