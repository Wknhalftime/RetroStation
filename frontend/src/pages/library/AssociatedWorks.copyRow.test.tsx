// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";

vi.mock("@/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/api/client")>();
  return { ...actual, apiFetch: vi.fn() };
});

import { ApiError, apiFetch } from "@/api/client";
import { AssociatedWorks } from "./AssociatedWorks";

// Phase 3b PR 5a (SPEC S8, D41 a): each copy row keeps what "Recordings & Files" showed of a
// file (the retired WorkFilesTable's tests): a missing or deleted copy is badged and can't be
// crowned, the album each copy is from, the cue editor for a copy on disk, and another work's
// copy, whose crown moves it into this song as it makes it the master.

const mockedApiFetch = vi.mocked(apiFetch);
const WORK_URL = "/api/v1/library/works/w-pf";
const TITLE = "Girl, You'll Be a Woman Soon";

const SOUNDTRACK = "11111111-1111-4111-8111-111111111111";
const HITS = "22222222-2222-4222-8222-222222222222";
const GONE = "33333333-3333-4333-8333-333333333333";
const DELETED = "44444444-4444-4444-8444-444444444444";
const LOOSE = "55555555-5555-4555-8555-555555555555";
const ELSEWHERE = "66666666-6666-4666-8666-666666666666";

function file(id: string, name: string, extra: Record<string, unknown> = {}) {
  return {
    id,
    file_path: `D:\\Music\\${name}`,
    format: name.split(".").pop(),
    bitrate: 320,
    duration_ms: 190_000,
    track_title: TITLE,
    release_title: null,
    enrichment_status: "enriched",
    file_status: "present",
    ...extra,
  };
}

const COPIES = [
  file(SOUNDTRACK, "soundtrack.flac", { release_title: "Pulp Fiction" }),
  file(HITS, "hits.mp3", { release_title: "Greatest Hits" }),
  file(GONE, "gone.mp3", { file_status: "missing" }),
  file(DELETED, "deleted.mp3", { file_status: "deleted" }),
];

const OTHER = file(ELSEWHERE, "bother.flac", {
  other_work: {
    id: "w-bother",
    title: "Bother",
    artist_name: "Corey Taylor",
    artists: ["Corey Taylor"],
  },
});

const WORK = {
  id: "w-pf",
  title: TITLE,
  artist_id: "a-urge",
  artists: [{ id: "a-urge", name: "Urge Overkill" }],
  recordings: [
    {
      id: "rec-1",
      title: TITLE,
      version_type: "original",
      duration_ms: 190_000,
      files: [...COPIES, file(LOOSE, "loose.mp3"), OTHER],
    },
  ],
  song_master: {
    id: "77777777-7777-4777-8777-777777777777",
    preferred_file_id: SOUNDTRACK,
    selection_method: "manual",
    score: null,
    updated_at: "2026-11-20T12:00:00Z",
  },
  format_overrides: [],
  performances: [
    {
      id: "p-1",
      credit: [{ id: "a-urge", name: "Urge Overkill" }],
      credit_text: "Urge Overkill",
      version_type: "original",
      default: true,
      preferred_copy_id: SOUNDTRACK,
      lines: 0,
      copies: COPIES.map((f) => ({ file: f, pins: [] })),
    },
  ],
  acts: [],
};

function serve() {
  mockedApiFetch.mockImplementation((url: string, options?: RequestInit) => {
    if (options?.method) return Promise.resolve({});
    if (url === WORK_URL) return Promise.resolve(WORK);
    return Promise.reject(new ApiError(404, `unexpected ${url}`));
  });
}

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/library/works/w-pf"]}>
        <Routes>
          <Route path="/library/works/:work_id" element={<AssociatedWorks />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

const row = async (name: string) => screen.findByRole("listitem", { name });

describe("AssociatedWorks, a copy's row", () => {
  beforeEach(() => {
    mockedApiFetch.mockReset();
  });

  it("badges a missing or deleted copy and offers no master action on it", async () => {
    serve();
    renderPage();

    for (const name of ["gone.mp3", "deleted.mp3"]) {
      const copy = await row(name);
      expect(within(copy).getByText("Missing")).toBeTruthy();
      expect(within(copy).queryByRole("button", { name: /master file/ })).toBeNull();
    }
    expect(within(await row("hits.mp3")).queryByText("Missing")).toBeNull();
  });

  it("names the album each copy is from, and none without an album tag", async () => {
    serve();
    renderPage();

    expect((await row("soundtrack.flac")).textContent).toContain("Pulp Fiction");
    expect((await row("hits.mp3")).textContent).toContain("Greatest Hits");
    expect(within(await row("loose.mp3")).queryByTestId("file-album")).toBeNull();
  });

  it("links a copy on disk to its cue editor, and a missing one to none", async () => {
    serve();
    renderPage();

    const cues = within(await row("hits.mp3")).getByRole("link", { name: "Cues" });
    expect(cues.getAttribute("href")).toBe(`/library/files/${HITS}/cues`);
    expect(within(await row("gone.mp3")).queryByRole("link", { name: "Cues" })).toBeNull();
    expect(within(await row("deleted.mp3")).queryByRole("link", { name: "Cues" })).toBeNull();
  });

  it("crowns another work's copy by moving it into this song", async () => {
    serve();
    renderPage();

    const other = await row("bother.flac");
    fireEvent.click(
      within(other).getByRole("button", { name: "Move into this song and set as master file" })
    );

    expect(mockedApiFetch).toHaveBeenCalledWith(`${WORK_URL}/master`, {
      method: "PUT",
      body: JSON.stringify({ preferred_file_id: ELSEWHERE }),
    });
  });

  it("marks the master's copy and keeps every other present copy crownable", async () => {
    serve();
    renderPage();

    const master = await row("soundtrack.flac");
    expect(within(master).getByRole("button", { name: "Current master file" })).toBeTruthy();
    expect(
      within(await row("hits.mp3")).getByRole("button", { name: "Set as master file" })
    ).toBeTruthy();
  });
});
