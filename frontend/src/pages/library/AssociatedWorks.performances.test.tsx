// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";

vi.mock("@/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/api/client")>();
  return { ...actual, apiFetch: vi.fn() };
});

import { ApiError, apiFetch } from "@/api/client";
import { AssociatedWorks } from "./AssociatedWorks";

// Phase 3b PR 5a (SPEC S8, D41 a, g, h; C71): the work page shows performances, each a block
// with its credit, a "default" mark on the master's and its copies, in place of "Recordings &
// Files". Set master stays on each copy. Copies with no performance yet (before 3a's build) and
// other works' copies (a shared recording) are listed apart. The header names the work's
// credited artists; another work is named by its own.

const mockedApiFetch = vi.mocked(apiFetch);
const WORK_URL = "/api/v1/library/works/w-bj";
const TITLE = "Who Says You Can't Go Home";

const MASTER = "11111111-1111-4111-8111-111111111111";
const DUET_MP3 = "22222222-2222-4222-8222-222222222222";
const DUET_FLAC = "33333333-3333-4333-8333-333333333333";
const LOOSE = "44444444-4444-4444-8444-444444444444";
const ELSEWHERE = "55555555-5555-4555-8555-555555555555";

function file(id: string, name: string, extra: Record<string, unknown> = {}) {
  return {
    id,
    file_path: `D:\\Music\\${name}`,
    format: name.split(".").pop(),
    bitrate: 900,
    duration_ms: 240_000,
    track_title: TITLE,
    release_title: null,
    enrichment_status: "enriched",
    file_status: "present",
    ...extra,
  };
}

const OTHER_WORK = {
  id: "w-other",
  title: TITLE,
  artist_name: "Bon Jovi",
  artists: ["Bon Jovi", "Sugarland"],
};

const RECORDINGS = [
  {
    id: "rec-1",
    title: TITLE,
    version_type: "original",
    duration_ms: 240_000,
    files: [
      file(MASTER, "solo.flac"),
      file(DUET_MP3, "duet.mp3"),
      file(DUET_FLAC, "duet.flac"),
      file(LOOSE, "loose.flac"),
      file(ELSEWHERE, "sugarland.flac", { other_work: OTHER_WORK }),
    ],
  },
];

const BON_JOVI = { id: "a-bj", name: "Bon Jovi" };
const NETTLES = { id: "a-jn", name: "Jennifer Nettles" };

const WORK = {
  id: "w-bj",
  title: TITLE,
  artist_id: "a-bj",
  artists: [BON_JOVI, NETTLES],
  recordings: RECORDINGS,
  song_master: {
    id: "66666666-6666-4666-8666-666666666666",
    preferred_file_id: MASTER,
    selection_method: "auto",
    score: 120,
    updated_at: "2026-11-20T12:00:00Z",
  },
  format_overrides: [],
  performances: [
    {
      id: "p-solo",
      credit: [BON_JOVI],
      credit_text: "Bon Jovi",
      version_type: "original",
      default: true,
      preferred_copy_id: null,
      lines: 0,
      copies: [{ file: file(MASTER, "solo.flac"), pins: [] }],
    },
    {
      id: "p-duet",
      credit: [BON_JOVI, NETTLES],
      credit_text: "Bon Jovi, Jennifer Nettles",
      version_type: "original",
      default: false,
      preferred_copy_id: DUET_FLAC,
      lines: 2,
      copies: [
        { file: file(DUET_MP3, "duet.mp3"), pins: [] },
        { file: file(DUET_FLAC, "duet.flac"), pins: [] },
      ],
    },
  ],
  acts: [],
};

function serve(work: unknown = WORK) {
  mockedApiFetch.mockImplementation((url: string, options?: RequestInit) => {
    if (options?.method) return Promise.resolve({});
    if (url === WORK_URL) return Promise.resolve(work);
    if (url === "/api/v1/library/artists/a-bj") return Promise.resolve(BON_JOVI);
    return Promise.reject(new ApiError(404, `unexpected ${url}`));
  });
}

function renderPage(path = "/library/works/w-bj") {
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

describe("AssociatedWorks, performances and their copies", () => {
  beforeEach(() => {
    mockedApiFetch.mockReset();
  });

  it("shows each performance as a block named by its credit, the master's marked default", async () => {
    serve();
    renderPage();

    const solo = await block("Bon Jovi");
    const duet = await block("Bon Jovi, Jennifer Nettles");

    expect(within(solo).getByText("Default")).toBeTruthy();
    expect(within(duet).queryByText("Default")).toBeNull();
    expect(within(solo).getByRole("listitem", { name: "solo.flac" })).toBeTruthy();
    expect(within(duet).getByRole("listitem", { name: "duet.mp3" })).toBeTruthy();
    expect(within(duet).getByRole("listitem", { name: "duet.flac" })).toBeTruthy();
  });

  it("no longer shows the Recordings & Files table", async () => {
    serve();
    renderPage();

    await block("Bon Jovi");
    expect(screen.queryByRole("heading", { name: /Recordings/ })).toBeNull();
  });

  it("keeps Set master on each copy and sends that copy", async () => {
    serve();
    renderPage();

    const solo = await block("Bon Jovi");
    expect(within(solo).getByRole("button", { name: "Current master file" })).toBeTruthy();
    const duet = await block("Bon Jovi, Jennifer Nettles");
    const flac = within(duet).getByRole("listitem", { name: "duet.flac" });
    fireEvent.click(within(flac).getByRole("button", { name: "Set as master file" }));

    await waitFor(() =>
      expect(mockedApiFetch).toHaveBeenCalledWith(`${WORK_URL}/master`, {
        method: "PUT",
        body: JSON.stringify({ preferred_file_id: DUET_FLAC }),
      })
    );
  });

  it("names the work's credited artists under its title", async () => {
    serve();
    renderPage();

    const heading = await screen.findByRole("heading", { name: TITLE, level: 1 });
    expect(heading.parentElement?.textContent).toContain("Bon Jovi, Jennifer Nettles");
  });

  it("lists a copy with no performance yet apart, with Set master", async () => {
    serve();
    renderPage();

    const loose = await block("Not in a performance yet");
    const row = within(loose).getByRole("listitem", { name: "loose.flac" });
    expect(within(row).getByRole("button", { name: "Set as master file" })).toBeTruthy();
    expect(within(loose).queryByRole("listitem", { name: "solo.flac" })).toBeNull();
    expect(within(loose).queryByRole("listitem", { name: "sugarland.flac" })).toBeNull();
  });

  it("names another work's copy by that work's credited artists, linking to its page", async () => {
    serve();
    renderPage();

    const elsewhere = await block("On other works");
    const row = within(elsewhere).getByRole("listitem", { name: "sugarland.flac" });
    expect(row.textContent).toContain(`on Bon Jovi, Sugarland › ${TITLE}`);
    const link = within(row).getByRole("link", { name: /Bon Jovi, Sugarland/ });
    expect(link.getAttribute("href")).toBe("/library/works/w-other");
  });

  it("shows every file of a work with no performance yet, from its recordings", async () => {
    const before = {
      id: "w-bj",
      title: TITLE,
      artist_id: "a-bj",
      recordings: RECORDINGS,
      song_master: null,
      format_overrides: [],
    };
    serve(before);
    renderPage();

    const loose = await block("Not in a performance yet");
    for (const name of ["solo.flac", "duet.mp3", "duet.flac", "loose.flac"]) {
      expect(within(loose).getByRole("listitem", { name })).toBeTruthy();
    }
    expect(screen.queryByRole("region", { name: "Bon Jovi" })).toBeNull();
  });
});
