// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";

vi.mock("@/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/api/client")>();
  return { ...actual, apiFetch: vi.fn() };
});

import { ApiError, apiFetch } from "@/api/client";
import { AssociatedWorks } from "./AssociatedWorks";

// Phase 3b PR 5b (SPEC S7, D47, D50; AUD-R088). A work's artists are its performances' credits,
// so a work whose performances credit no one (its artist was the Various Artists placeholder,
// which AUD-R078 never credits) has none. Wherever the page names such a performance or work it
// reads "No credited artist": a performance block's heading and region (D47); another work's
// copy under "On other works" (D50); and the header, when the address names no artist (D50).
// The detail has no `artist_id` and another work no `artist_name` (S7): frontend text only.

const mockedApiFetch = vi.mocked(apiFetch);
const TITLE = "Lady Marmalade";
const COMP = "11111111-1111-4111-8111-111111111111";
const LIVE = "22222222-2222-4222-8222-222222222222";
const ELSEWHERE = "33333333-3333-4333-8333-333333333333";
const NO_CREDIT = "No credited artist";

function file(id: string, name: string, other: object | null = null) {
  return {
    id,
    file_path: `D:\\Music\\${name}`,
    format: name.endsWith(".flac") ? "flac" : "mp3",
    bitrate: 900,
    duration_ms: 270_000,
    track_title: TITLE,
    release_title: null,
    enrichment_status: "enriched",
    file_status: "present",
    other_work: other,
  };
}

function performance(id: string, copy: ReturnType<typeof file>, version = "original") {
  return {
    id,
    credit: [],
    credit_text: "",
    version_type: version,
    default: version === "original",
    preferred_copy_id: copy.id,
    lines: 0,
    copies: [{ file: copy, pins: [] }],
  };
}

const COMP_FILE = file(COMP, "comp.flac");
const LIVE_FILE = file(LIVE, "live.mp3");
const OTHER_FILE = file(ELSEWHERE, "patti.flac", { id: "w-other", title: TITLE, artists: [] });

const UNCREDITED = {
  id: "w-va",
  title: TITLE,
  artists: [],
  recordings: [
    {
      id: "rec-1",
      title: TITLE,
      version_type: "original",
      duration_ms: 270_000,
      files: [COMP_FILE, LIVE_FILE, OTHER_FILE],
    },
  ],
  song_master: {
    id: "44444444-4444-4444-8444-444444444444",
    preferred_file_id: COMP,
    selection_method: "auto",
    score: 120,
    updated_at: "2026-11-20T00:00:00Z",
  },
  format_overrides: [],
  performances: [performance("p-comp", COMP_FILE), performance("p-live", LIVE_FILE, "live")],
  acts: [],
};

function serve(detail: object = UNCREDITED) {
  mockedApiFetch.mockImplementation((url: string) => {
    if (url === "/api/v1/library/works/w-va") return Promise.resolve(detail);
    if (url === "/api/v1/library/artists/a-va") {
      return Promise.resolve({ id: "a-va", name: "Various Artists" });
    }
    return Promise.reject(new ApiError(404, `unexpected ${url}`));
  });
}

function renderAt(path: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/library/works/:work_id" element={<AssociatedWorks />} />
          <Route path="/library/artists/:artist_id/works/:work_id" element={<AssociatedWorks />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

const block = (name: string) => screen.findByRole("region", { name });

describe("AssociatedWorks, a work with no credited artist", () => {
  beforeEach(() => {
    mockedApiFetch.mockReset();
  });

  it("names a performance with no credit 'No credited artist' (D47)", async () => {
    serve();
    renderAt("/library/works/w-va");

    const comp = await block(NO_CREDIT);
    expect(within(comp).getByText(NO_CREDIT)).toBeTruthy();
    expect(within(comp).getByRole("listitem", { name: "comp.flac" })).toBeTruthy();
    expect(within(comp).getByText("Default")).toBeTruthy();
  });

  it("keeps the version beside it for another version (D47)", async () => {
    serve();
    renderAt("/library/works/w-va");

    const live = await block(`${NO_CREDIT} (live)`);
    expect(within(live).getByRole("listitem", { name: "live.mp3" })).toBeTruthy();
  });

  it("names another work with no credited artist the same way (D50)", async () => {
    serve();
    renderAt("/library/works/w-va");

    const elsewhere = await block("On other works");
    const row = within(elsewhere).getByRole("listitem", { name: "patti.flac" });
    expect(row.textContent).toContain(`on ${NO_CREDIT} › ${TITLE}`);
    const link = within(row).getByRole("link", { name: new RegExp(NO_CREDIT) });
    expect(link.getAttribute("href")).toBe("/library/works/w-other");
  });

  it("shows 'No credited artist' in the header at the work's own address (D50)", async () => {
    serve();
    renderAt("/library/works/w-va");

    const heading = await screen.findByRole("heading", { name: TITLE, level: 1 });
    expect(heading.parentElement?.textContent).toContain(NO_CREDIT);
  });

  it("keeps the address's artist in the header when the address names one", async () => {
    serve();
    renderAt("/library/artists/a-va/works/w-va");

    const heading = await screen.findByRole("heading", { name: TITLE, level: 1 });
    await screen.findByRole("link", { name: "Various Artists" });
    expect(heading.parentElement?.textContent).toContain("Various Artists");
    expect(heading.parentElement?.textContent).not.toContain(NO_CREDIT);
  });

  it("names credited performances by their credit, as before", async () => {
    const credited = {
      ...UNCREDITED,
      artists: [{ id: "a-labelle", name: "Labelle" }],
      performances: [
        {
          ...performance("p-comp", COMP_FILE),
          credit: [{ id: "a-labelle", name: "Labelle" }],
          credit_text: "Labelle",
        },
      ],
    };
    serve(credited);
    renderAt("/library/works/w-va");

    expect(await block("Labelle")).toBeTruthy();
    expect(screen.queryByRole("region", { name: NO_CREDIT })).toBeNull();
    const heading = screen.getByRole("heading", { name: TITLE, level: 1 });
    expect(heading.parentElement?.textContent).toContain("Labelle");
  });
});
