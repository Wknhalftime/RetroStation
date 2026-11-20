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

// Phase 3b PR 5a (SPEC S8, D41 d; C72): under each copy, the lines it holds through a merge
// pin, collapsed as "Pinned lines (N)", N counting lines. Each line shows its stations, plays
// and what it plays next; Release frees the line at once (the line then takes the normal path).

const mockedApiFetch = vi.mocked(apiFetch);
const WORK_URL = "/api/v1/library/works/w-sugar";
const TITLE = "Pour Some Sugar On Me";

const MASTER = "11111111-1111-4111-8111-111111111111";
const MERGED = "22222222-2222-4222-8222-222222222222";
const EDIT = "33333333-3333-4333-8333-333333333333";

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

function pin(
  identity: string,
  title: string,
  station: string,
  plays: number,
  next: [string, string] | null
) {
  return {
    identity_id: identity,
    station_id: `s-${station.toLowerCase()}`,
    station,
    title,
    artist: "DEF LEPPARD",
    plays,
    next_file_id: next ? next[0] : null,
    next_path: next ? `D:\\Music\\${next[1]}` : null,
  };
}

const PINS = [
  pin("i-1", TITLE, "KISW", 3, [MASTER, "master.flac"]),
  pin("i-1", TITLE, "KZOK", 2, [EDIT, "edit.mp3"]),
  pin("i-2", `${TITLE} (Remix)`, "KISW", 1, null),
];

const WORK = {
  id: "w-sugar",
  title: TITLE,
  artist_id: "a-dl",
  artists: [{ id: "a-dl", name: "Def Leppard" }],
  recordings: [],
  song_master: {
    id: "66666666-6666-4666-8666-666666666666",
    preferred_file_id: MASTER,
    selection_method: "manual",
    score: null,
    updated_at: "2026-11-20T12:00:00Z",
  },
  format_overrides: [],
  performances: [
    {
      id: "p-1",
      credit: [{ id: "a-dl", name: "Def Leppard" }],
      credit_text: "Def Leppard",
      version_type: "original",
      default: true,
      preferred_copy_id: MASTER,
      lines: 0,
      copies: [
        { file: file(MASTER, "master.flac"), pins: [] },
        { file: file(MERGED, "merged.mp3"), pins: PINS },
        { file: file(EDIT, "edit.mp3"), pins: [] },
      ],
    },
  ],
  acts: [],
};

function serve(write: () => Promise<unknown> = () => Promise.resolve({ released: 1 })) {
  mockedApiFetch.mockImplementation((url: string, options?: RequestInit) => {
    if (options?.method) return write();
    if (url === WORK_URL) return Promise.resolve(WORK);
    return Promise.reject(new ApiError(404, `unexpected ${url}`));
  });
}

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/library/works/w-sugar"]}>
        <Routes>
          <Route path="/library/works/:work_id" element={<AssociatedWorks />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

async function mergedCopy() {
  const block = await screen.findByRole("region", { name: "Def Leppard" });
  return within(block).getByRole("listitem", { name: "merged.mp3" });
}

async function expand() {
  const copy = await mergedCopy();
  fireEvent.click(within(copy).getByRole("button", { name: "Pinned lines (2)" }));
  return copy;
}

describe("AssociatedWorks, pinned lines", () => {
  beforeEach(() => {
    mockedApiFetch.mockReset();
  });

  it("shows a copy's pins collapsed, counted by line", async () => {
    serve();
    renderPage();

    const copy = await mergedCopy();
    const toggle = within(copy).getByRole("button", { name: "Pinned lines (2)" });
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(within(copy).queryByRole("button", { name: `Release ${TITLE}` })).toBeNull();
  });

  it("shows each line's stations, plays and what it plays next", async () => {
    serve();
    renderPage();

    const copy = await expand();
    const line = within(copy).getByRole("listitem", { name: TITLE });
    expect(line.textContent).toContain("KISW");
    expect(line.textContent).toContain("3 plays");
    expect(line.textContent).toContain("plays next: master.flac");
    expect(line.textContent).toContain("KZOK");
    expect(line.textContent).toContain("2 plays");
    expect(line.textContent).toContain("plays next: edit.mp3");
  });

  it("says when a line plays nothing next", async () => {
    serve();
    renderPage();

    const copy = await expand();
    const remix = within(copy).getByRole("listitem", { name: `${TITLE} (Remix)` });
    expect(remix.textContent).toContain("1 play");
    expect(remix.textContent).not.toContain("1 plays");
    expect(remix.textContent).toContain("plays next: nothing");
  });

  it("releases a line at once, with no confirm", async () => {
    serve();
    renderPage();

    const copy = await expand();
    fireEvent.click(within(copy).getByRole("button", { name: `Release ${TITLE}` }));

    await waitFor(() =>
      expect(mockedApiFetch).toHaveBeenCalledWith(`${WORK_URL}/pins/release`, {
        method: "POST",
        body: JSON.stringify({ identity_ids: ["i-1"] }),
      })
    );
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("shows no pinned lines on a copy that holds none", async () => {
    serve();
    renderPage();

    await mergedCopy();
    const master = screen.getByRole("listitem", { name: "master.flac" });
    expect(within(master).queryByRole("button", { name: /Pinned lines/ })).toBeNull();
  });

  it("shows why a release failed", async () => {
    serve(() => Promise.reject(new ApiError(422, "Line i-1 pins no copy of work w-sugar")));
    renderPage();

    const copy = await expand();
    fireEvent.click(within(copy).getByRole("button", { name: `Release ${TITLE}` }));

    expect((await screen.findByRole("alert")).textContent).toContain(
      "Line i-1 pins no copy of work w-sugar"
    );
  });
});
