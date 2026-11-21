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

// Phase 3b PR 5a (SPEC S8, D41 f; design C65): on a work whose performances credit two or more
// acts, a notice in the Format Overrides add form and beside the Versions map control names
// each act and the lines an override or version entry reaches: the default act's, on the
// default path; another act's lines keep their own performance (it plays first, C61). In the
// add form, the format typed narrows it to that format's stations.

const mockedApiFetch = vi.mocked(apiFetch);
const WORK_URL = "/api/v1/library/works/w-lm";
const TITLE = "Lady Marmalade";

const MASTER = "11111111-1111-4111-8111-111111111111";
const COVER = "22222222-2222-4222-8222-222222222222";

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

const LABELLE = { id: "a-lb", name: "LaBelle" };
const AGUILERA = { id: "a-ca", name: "Christina Aguilera" };
const KIM = { id: "a-lk", name: "Lil' Kim" };

const ACTS = [
  { credit: "LaBelle", default: true, lines: 3, reached: { AOR: 3, "Classic Hits": 1 } },
  { credit: "Christina Aguilera, Lil' Kim", default: false, lines: 1, reached: {} },
];

function work(acts: unknown[]) {
  return {
    id: "w-lm",
    title: TITLE,
    artist_id: "a-lb",
    artists: [LABELLE, AGUILERA, KIM],
    recordings: [
      {
        id: "rec-1",
        title: TITLE,
        version_type: "original",
        duration_ms: 240_000,
        files: [file(MASTER, "labelle.flac"), file(COVER, "aguilera.flac")],
      },
    ],
    song_master: {
      id: "66666666-6666-4666-8666-666666666666",
      preferred_file_id: MASTER,
      selection_method: "auto",
      score: 120,
      updated_at: "2026-11-20T12:00:00Z",
    },
    format_overrides: [],
    versions: [
      {
        version_text: "live",
        version_type: "live",
        reads_as_version: true,
        state: "new",
        mapped_file_id: null,
        plays: 4,
        stations: ["KISW"],
        first_seen: "2026-11-01",
        first_station: "KISW",
        new: true,
        suggestion: null,
      },
    ],
    master_flag: null,
    performances: [
      {
        id: "p-lb",
        credit: [LABELLE],
        credit_text: "LaBelle",
        version_type: "original",
        default: true,
        preferred_copy_id: MASTER,
        lines: 0,
        copies: [{ file: file(MASTER, "labelle.flac"), pins: [] }],
      },
      {
        id: "p-ca",
        credit: [AGUILERA, KIM],
        credit_text: "Christina Aguilera, Lil' Kim",
        version_type: "original",
        default: false,
        preferred_copy_id: COVER,
        lines: 1,
        copies: [{ file: file(COVER, "aguilera.flac"), pins: [] }],
      },
    ],
    acts,
  };
}

function serve(detail: unknown) {
  mockedApiFetch.mockImplementation((url: string, options?: RequestInit) => {
    if (options?.method) return Promise.resolve({});
    if (url === WORK_URL) return Promise.resolve(detail);
    return Promise.reject(new ApiError(404, `unexpected ${url}`));
  });
}

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/library/works/w-lm"]}>
        <Routes>
          <Route path="/library/works/:work_id" element={<AssociatedWorks />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

async function openOverrideForm() {
  fireEvent.click(await screen.findByRole("button", { name: /Add Override/ }));
}

function overrideNotice() {
  const form = screen.getByRole("form", { name: "Add override" });
  return within(form).getByRole("note", { name: "Two acts" });
}

describe("AssociatedWorks, a work holding two acts", () => {
  beforeEach(() => {
    mockedApiFetch.mockReset();
  });

  it("names each act and the lines an override reaches, in the add form", async () => {
    serve(work(ACTS));
    renderPage();

    await openOverrideForm();

    const notice = overrideNotice();
    expect(notice.textContent).toContain("LaBelle");
    expect(notice.textContent).toMatch(/reaches 3 lines of LaBelle/);
    expect(notice.textContent).toContain("Christina Aguilera, Lil' Kim");
    expect(notice.textContent).toMatch(/1 line of Christina Aguilera, Lil' Kim keeps its own/);
  });

  it("narrows it to the stations of the format typed", async () => {
    serve(work(ACTS));
    renderPage();

    await openOverrideForm();
    const format = screen.getByPlaceholderText("e.g. FLAC, MP3");

    fireEvent.change(format, { target: { value: "Classic Hits" } });
    expect(overrideNotice().textContent).toMatch(/at Classic Hits stations: 1 line\b/);

    fireEvent.change(format, { target: { value: "AOR" } });
    expect(overrideNotice().textContent).toMatch(/at AOR stations: 3 lines/);

    fireEvent.change(format, { target: { value: "Jazz" } });
    expect(overrideNotice().textContent).toMatch(/at Jazz stations: no lines/);
  });

  it("shows the same notice beside the Versions map control", async () => {
    serve(work(ACTS));
    renderPage();

    const control = await screen.findByRole("group", { name: "Map versions" });
    expect(within(control).getByRole("combobox", { name: "Map to file" })).toBeTruthy();
    const notice = within(control).getByRole("note", { name: "Two acts" });
    expect(notice.textContent).toMatch(/reaches 3 lines of LaBelle/);
    expect(notice.textContent).toContain("Christina Aguilera, Lil' Kim");
  });

  it("shows no notice on a work holding one act", async () => {
    serve(work([]));
    renderPage();

    await openOverrideForm();
    await screen.findByRole("combobox", { name: "Map to file" });

    expect(screen.queryByRole("note", { name: "Two acts" })).toBeNull();
  });
});
