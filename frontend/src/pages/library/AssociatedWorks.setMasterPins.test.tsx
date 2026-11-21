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

// Phase 3b PR 5a (SPEC S8, D41 e; C72, Lance 2026-10-29): before Set master replaces a copy
// that holds pins, the page lists those pins, each line with a tick, unticked: a kept pin stays
// on the replaced copy. The ticked lines are released first, then the master is set (PUT
// master's locked body); releasing first changes nothing until the master moves.

const mockedApiFetch = vi.mocked(apiFetch);
const WORK_URL = "/api/v1/library/works/w-alone";
const TITLE = "Alone";

const OLD = "11111111-1111-4111-8111-111111111111";
const NEW = "22222222-2222-4222-8222-222222222222";

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

function pin(identity: string, title: string, station: string, plays: number) {
  return {
    identity_id: identity,
    station_id: `s-${station.toLowerCase()}`,
    station,
    title,
    artist: "HEART",
    plays,
    next_file_id: OLD,
    next_path: "D:\\Music\\old.flac",
  };
}

function work(oldPins: unknown[]) {
  return {
    id: "w-alone",
    title: TITLE,
    artist_id: "a-heart",
    artists: [{ id: "a-heart", name: "Heart" }],
    recordings: [],
    song_master: {
      id: "66666666-6666-4666-8666-666666666666",
      preferred_file_id: OLD,
      selection_method: "manual",
      score: null,
      updated_at: "2026-11-20T12:00:00Z",
    },
    format_overrides: [],
    performances: [
      {
        id: "p-1",
        credit: [{ id: "a-heart", name: "Heart" }],
        credit_text: "Heart",
        version_type: "original",
        default: true,
        preferred_copy_id: OLD,
        lines: 0,
        copies: [
          { file: file(OLD, "old.flac"), pins: oldPins },
          { file: file(NEW, "new.mp3"), pins: [] },
        ],
      },
    ],
    acts: [],
  };
}

const PINNED = work([
  pin("i-1", TITLE, "KJR", 4),
  pin("i-1", TITLE, "KISW", 1),
  pin("i-2", `${TITLE} (Live)`, "KJR", 2),
]);

function serve(detail: unknown, release: () => Promise<unknown> = () => Promise.resolve()) {
  mockedApiFetch.mockImplementation((url: string, options?: RequestInit) => {
    if (options?.method === "POST") return release();
    if (options?.method) return Promise.resolve({});
    if (url === WORK_URL) return Promise.resolve(detail);
    return Promise.reject(new ApiError(404, `unexpected ${url}`));
  });
}

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/library/works/w-alone"]}>
        <Routes>
          <Route path="/library/works/:work_id" element={<AssociatedWorks />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

/** The writes the page sent, in order, as (method, url, body). */
function writes(): [string, string, unknown][] {
  return mockedApiFetch.mock.calls
    .filter(([, options]) => options?.method)
    .map(([url, options]) => [
      String(options?.method),
      url,
      options?.body ? JSON.parse(String(options.body)) : null,
    ]);
}

async function chooseNew() {
  const block = await screen.findByRole("region", { name: "Heart" });
  const row = within(block).getByRole("listitem", { name: "new.mp3" });
  fireEvent.click(within(row).getByRole("button", { name: "Set as master file" }));
}

const MASTER_PUT: [string, string, unknown] = [
  "PUT",
  `${WORK_URL}/master`,
  { preferred_file_id: NEW },
];

describe("AssociatedWorks, Set master over a copy that holds pins", () => {
  beforeEach(() => {
    mockedApiFetch.mockReset();
  });

  it("first lists the replaced copy's pinned lines, each unticked", async () => {
    serve(PINNED);
    renderPage();

    await chooseNew();

    const dialog = screen.getByRole("dialog", { name: "Set master" });
    const boxes = within(dialog).getAllByRole("checkbox");
    expect(boxes).toHaveLength(2);
    expect(boxes.every((b) => !(b as HTMLInputElement).checked)).toBe(true);
    // One tick per line, named by its title and its stations.
    expect(within(dialog).getByRole("checkbox", { name: `${TITLE} on KJR, KISW` })).toBeTruthy();
    expect(within(dialog).getByRole("checkbox", { name: `${TITLE} (Live) on KJR` })).toBeTruthy();
    expect(dialog.textContent).toContain("KJR");
    expect(dialog.textContent).toContain("4 plays");
    expect(writes()).toEqual([]);
  });

  it("says what a released line plays and where a kept one stays", async () => {
    serve(PINNED);
    renderPage();

    await chooseNew();

    const dialog = screen.getByRole("dialog", { name: "Set master" });
    expect(dialog.textContent).toContain("Released lines play new.mp3");
    expect(dialog.textContent).toContain("Kept lines stay on old.flac");
  });

  it("with nothing ticked, sets the master and releases nothing", async () => {
    serve(PINNED);
    renderPage();

    await chooseNew();
    const dialog = screen.getByRole("dialog", { name: "Set master" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Set master" }));

    await waitFor(() => expect(writes()).toEqual([MASTER_PUT]));
  });

  it("releases the ticked lines first, then sets the master", async () => {
    serve(PINNED);
    renderPage();

    await chooseNew();
    const dialog = screen.getByRole("dialog", { name: "Set master" });
    fireEvent.click(
      within(dialog).getByRole("checkbox", { name: new RegExp(`^${TITLE} \\(Live`) })
    );
    fireEvent.click(within(dialog).getByRole("button", { name: "Set master" }));

    await waitFor(() =>
      expect(writes()).toEqual([
        ["POST", `${WORK_URL}/pins/release`, { identity_ids: ["i-2"] }],
        MASTER_PUT,
      ])
    );
  });

  it("changes nothing on Cancel", async () => {
    serve(PINNED);
    renderPage();

    await chooseNew();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    expect(screen.queryByRole("dialog")).toBeNull();
    expect(writes()).toEqual([]);
  });

  it("sets the master at once when the replaced copy holds no pins", async () => {
    serve(work([]));
    renderPage();

    await chooseNew();

    expect(screen.queryByRole("dialog")).toBeNull();
    await waitFor(() => expect(writes()).toEqual([MASTER_PUT]));
  });

  it("sets no master when the release fails, and says why", async () => {
    serve(PINNED, () => Promise.reject(new ApiError(422, "Line i-1 pins no copy of work w-alone")));
    renderPage();

    await chooseNew();
    const dialog = screen.getByRole("dialog", { name: "Set master" });
    fireEvent.click(within(dialog).getByRole("checkbox", { name: new RegExp(`^${TITLE} on`) }));
    fireEvent.click(within(dialog).getByRole("button", { name: "Set master" }));

    expect((await screen.findByRole("alert")).textContent).toContain(
      "Line i-1 pins no copy of work w-alone"
    );
    expect(writes().map(([method]) => method)).toEqual(["POST"]);
  });
});
