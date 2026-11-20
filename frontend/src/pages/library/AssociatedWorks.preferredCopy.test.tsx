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

// Phase 3b PR 5a (SPEC S8, D41 c; C71): a performance some line stores says how many lines
// play it, and each of its copies but the preferred one offers "Prefer this copy", which takes
// effect at once, with no confirm. The default performance never offers it (Set master picks
// its copy), nor does a performance no line stores.

const mockedApiFetch = vi.mocked(apiFetch);
const WORK_URL = "/api/v1/library/works/w-bj";
const TITLE = "Who Says You Can't Go Home";

const MASTER = "11111111-1111-4111-8111-111111111111";
const DUET_MP3 = "22222222-2222-4222-8222-222222222222";
const DUET_FLAC = "33333333-3333-4333-8333-333333333333";
const LIVE = "44444444-4444-4444-8444-444444444444";
const SOLO_MP3 = "55555555-5555-4555-8555-555555555555";

function file(id: string, name: string) {
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
  };
}

const BON_JOVI = { id: "a-bj", name: "Bon Jovi" };
const NETTLES = { id: "a-jn", name: "Jennifer Nettles" };

function performance(
  id: string,
  credit: { id: string; name: string }[],
  copies: [string, string][],
  extra: Record<string, unknown> = {}
) {
  return {
    id,
    credit,
    credit_text: credit.map((c) => c.name).join(", "),
    version_type: "original",
    default: false,
    preferred_copy_id: null,
    lines: 0,
    copies: copies.map(([fid, name]) => ({ file: file(fid, name), pins: [] })),
    ...extra,
  };
}

function work(duetLines = 2) {
  return {
    id: "w-bj",
    title: TITLE,
    artist_id: "a-bj",
    artists: [BON_JOVI, NETTLES],
    recordings: [],
    song_master: {
      id: "66666666-6666-4666-8666-666666666666",
      preferred_file_id: MASTER,
      selection_method: "auto",
      score: 120,
      updated_at: "2026-11-20T12:00:00Z",
    },
    format_overrides: [],
    performances: [
      performance(
        "p-solo",
        [BON_JOVI],
        [
          [MASTER, "solo.flac"],
          [SOLO_MP3, "solo.mp3"],
        ],
        { default: true, preferred_copy_id: MASTER }
      ),
      performance(
        "p-duet",
        [BON_JOVI, NETTLES],
        [
          [DUET_MP3, "duet.mp3"],
          [DUET_FLAC, "duet.flac"],
        ],
        { preferred_copy_id: DUET_FLAC, lines: duetLines }
      ),
      performance("p-live", [NETTLES], [[LIVE, "live.mp3"]], {
        version_type: "live",
        preferred_copy_id: LIVE,
      }),
    ],
    acts: [],
  };
}

function serve(detail: unknown = work(), write: () => Promise<unknown> = () => Promise.resolve()) {
  mockedApiFetch.mockImplementation((url: string, options?: RequestInit) => {
    if (options?.method) return write();
    if (url === WORK_URL) return Promise.resolve(detail);
    return Promise.reject(new ApiError(404, `unexpected ${url}`));
  });
}

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/library/works/w-bj"]}>
        <Routes>
          <Route path="/library/works/:work_id" element={<AssociatedWorks />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

const duetBlock = () => screen.findByRole("region", { name: "Bon Jovi, Jennifer Nettles" });

describe("AssociatedWorks, Prefer this copy", () => {
  beforeEach(() => {
    mockedApiFetch.mockReset();
  });

  it("says how many lines play a stored performance", async () => {
    serve();
    renderPage();

    expect((await duetBlock()).textContent).toContain("2 lines play this performance");
  });

  it("says it in the singular for one line", async () => {
    serve(work(1));
    renderPage();

    expect((await duetBlock()).textContent).toContain("1 line plays this performance");
  });

  it("offers it on each copy but the preferred one, which is marked", async () => {
    serve();
    renderPage();

    const duet = await duetBlock();
    const mp3 = within(duet).getByRole("listitem", { name: "duet.mp3" });
    const flac = within(duet).getByRole("listitem", { name: "duet.flac" });
    expect(within(mp3).getByRole("button", { name: "Prefer this copy" })).toBeTruthy();
    expect(within(flac).queryByRole("button", { name: "Prefer this copy" })).toBeNull();
    expect(within(flac).getByText("Preferred copy")).toBeTruthy();
  });

  it("never offers it on the default performance or one no line stores", async () => {
    serve();
    renderPage();

    await duetBlock();
    const solo = screen.getByRole("region", { name: "Bon Jovi" });
    const live = screen.getByRole("region", { name: "Jennifer Nettles (live)" });
    expect(within(solo).queryByRole("button", { name: "Prefer this copy" })).toBeNull();
    expect(within(live).queryByRole("button", { name: "Prefer this copy" })).toBeNull();
    expect(live.textContent).not.toMatch(/play(s)? this performance/);
  });

  it("sends the copy at once, with no confirm", async () => {
    serve();
    renderPage();

    const duet = await duetBlock();
    const mp3 = within(duet).getByRole("listitem", { name: "duet.mp3" });
    fireEvent.click(within(mp3).getByRole("button", { name: "Prefer this copy" }));

    expect(mockedApiFetch).toHaveBeenCalledWith(`${WORK_URL}/performances/p-duet/preferred-copy`, {
      method: "PUT",
      body: JSON.stringify({ file_id: DUET_MP3 }),
    });
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("shows why a refusal failed", async () => {
    serve(work(), () => Promise.reject(new ApiError(422, `File ${DUET_MP3} is missing from disk`)));
    renderPage();

    const duet = await duetBlock();
    const mp3 = within(duet).getByRole("listitem", { name: "duet.mp3" });
    fireEvent.click(within(mp3).getByRole("button", { name: "Prefer this copy" }));

    expect((await screen.findByRole("alert")).textContent).toContain(
      `File ${DUET_MP3} is missing from disk`
    );
  });
});
