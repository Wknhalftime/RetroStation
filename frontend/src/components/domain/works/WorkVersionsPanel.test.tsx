// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import type { ReactNode } from "react";

vi.mock("@/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/api/client")>();
  return { ...actual, apiFetch: vi.fn() };
});

import { ApiError, ConflictError, apiFetch } from "@/api/client";
import { WorkVersionsPanel } from "./WorkVersionsPanel";
import { AssociatedWorks } from "@/pages/library/AssociatedWorks";

// Versions phase 2b PR 3, SPEC S6 (M30, C62, C64, C66): beside the format overrides, the work
// page lists the version texts of its matched lines. One or more can be mapped to a file of the
// work, a mapped one unmapped, an unmapped one dismissed; a suggestion is shown and applied only
// when pressed; a new one says "New version detected" with its first station and date; title
// words are listed below the versions, never warn and offer no Dismiss, but can be mapped (a
// venue the log reader doesn't know reads as title words); the master flag is shown. Each write sends the routes'
// request (S5) and the buttons wait while one is in flight.

const mockedApiFetch = vi.mocked(apiFetch);
const WORK = "w-sandman";
const ENTRIES = `/api/v1/library/works/${WORK}/version-entries`;
const DISMISS = `/api/v1/library/works/${WORK}/versions/dismiss`;
const F_MASTER = "00000000-0000-4000-8000-0000000000a1";
const F_LIVE = "00000000-0000-4000-8000-0000000000a2";
const F_MISSING = "00000000-0000-4000-8000-0000000000a3";
const F_THEIRS = "00000000-0000-4000-8000-0000000000a4";

function file(id: string, overrides: Record<string, unknown> = {}) {
  return {
    id,
    file_path: `D:/music/${id}.flac`,
    format: "flac",
    bitrate: 1000,
    duration_ms: 330_000,
    track_title: null,
    release_title: null,
    enrichment_status: "enriched",
    file_status: "present" as const,
    other_work: null,
    ...overrides,
  };
}

const RECORDINGS = [
  {
    id: "r-original",
    title: "Enter Sandman",
    version_type: "original",
    duration_ms: 330_000,
    files: [
      file(F_MASTER),
      file(F_MISSING, { file_status: "missing" as const }),
      file(F_THEIRS, { other_work: { id: "w-other", title: "Other", artist_name: "Other" } }),
    ],
  },
  {
    id: "r-live",
    title: "Enter Sandman (Live at Seattle)",
    version_type: "live",
    duration_ms: 360_000,
    files: [file(F_LIVE)],
  },
];

type Version = ReturnType<typeof version>;

function version(text: string, overrides: Record<string, unknown> = {}) {
  return {
    version_text: text,
    version_type: "live",
    reads_as_version: true,
    state: "unmapped" as "unmapped" | "mapped" | "dismissed",
    mapped_file_id: null as string | null,
    plays: 10,
    stations: ["KISW"],
    first_seen: "2001-05-03" as string | null,
    first_station: "KISW" as string | null,
    new: false,
    suggestion: null as null | {
      file_id: string;
      reason: "matched_file" | "only_copy";
      track_title: string | null;
      file_path: string;
    },
    ...overrides,
  };
}

const LIVE = version("live", { plays: 120, stations: ["KISW", "WXRT"] });
const LV = version("lv", {
  plays: 7,
  new: true,
  first_seen: "2026-11-02",
  first_station: "WXRT",
  stations: ["WXRT"],
});
const DEMO = version("demo", { version_type: "demo", plays: 3 });
const MAPPED = version("unplugged", {
  version_type: "acoustic",
  state: "mapped",
  mapped_file_id: F_LIVE,
});
const DISMISSED = version("acoustic", { version_type: "acoustic", state: "dismissed" });
const PART = version("part 1", { version_type: "unknown", reads_as_version: false, plays: 40 });

let qc: QueryClient;

function wrapper({ children }: { children: ReactNode }) {
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  );
}

function renderPanel(versions: Version[], masterFlag: unknown = null) {
  render(
    <WorkVersionsPanel
      workId={WORK}
      versions={versions}
      masterFlag={masterFlag as never}
      recordings={RECORDINGS}
    />,
    { wrapper }
  );
}

function writes(): [string, string, unknown][] {
  return mockedApiFetch.mock.calls
    .filter(([, init]) => (init as RequestInit | undefined)?.method !== undefined)
    .map(([url, init]) => {
      const options = init as RequestInit;
      return [url, options.method ?? "GET", options.body ? JSON.parse(String(options.body)) : null];
    });
}

beforeEach(() => {
  qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  mockedApiFetch.mockReset();
  mockedApiFetch.mockResolvedValue(undefined);
});

describe("WorkVersionsPanel: the list", () => {
  it("lists each version with its plays, stations and reading, then the title words", () => {
    renderPanel([LIVE, LV, DEMO, PART]);

    expect(screen.getByRole("heading", { name: "Versions" })).toBeTruthy();
    const live = screen.getByRole("listitem", { name: "live" });
    expect(live.textContent).toContain("120");
    expect(live.textContent).toContain("KISW");
    expect(live.textContent).toContain("WXRT");
    const titleWords = screen.getByRole("heading", { name: "Title words" });
    const part = screen.getByRole("listitem", { name: "part 1" });
    expect(
      titleWords.compareDocumentPosition(part) & Node.DOCUMENT_POSITION_FOLLOWING
    ).toBeTruthy();
    expect(
      screen.getByRole("listitem", { name: "demo" }).compareDocumentPosition(titleWords) &
        Node.DOCUMENT_POSITION_FOLLOWING
    ).toBeTruthy();
    // Title words never warn, so they offer no Dismiss; they can still be mapped.
    expect(within(part).queryByRole("button", { name: /Dismiss/ })).toBeNull();
    expect(within(part).getByRole("checkbox", { name: "Select part 1" })).toBeTruthy();
  });

  it("says New version detected, with its first station and date, on a new one only", () => {
    renderPanel([LIVE, LV, version("part 2", { reads_as_version: false, new: false })]);

    const lv = screen.getByRole("listitem", { name: "lv" });
    expect(lv.textContent).toContain("New version detected");
    expect(lv.textContent).toMatch(/WXRT/);
    expect(lv.textContent).toMatch(/2026-11-02/);
    expect(screen.getAllByText(/New version detected/)).toHaveLength(1);
  });

  it("shows each state, and a mapped one's file", () => {
    renderPanel([LIVE, MAPPED, DISMISSED]);

    expect(screen.getByRole("listitem", { name: "live" }).textContent).toContain("Unmapped");
    const mapped = screen.getByRole("listitem", { name: "unplugged" });
    expect(mapped.textContent).toContain("Mapped");
    expect(mapped.textContent).toContain("Enter Sandman (Live at Seattle)");
    expect(screen.getByRole("listitem", { name: "acoustic" }).textContent).toContain("Dismissed");
  });

  it("says so when the work has no version texts", () => {
    renderPanel([]);

    expect(screen.getByText(/No version texts/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Map selected" })).toBeNull();
  });
});

describe("WorkVersionsPanel: map, unmap, dismiss", () => {
  it("maps one or more selected texts to a present file of this work", async () => {
    renderPanel([LIVE, LV, DEMO, DISMISSED]);
    const mapButton = screen.getByRole("button", { name: "Map selected" }) as HTMLButtonElement;
    const picker = screen.getByRole("combobox", { name: "Map to file" }) as HTMLSelectElement;

    expect(mapButton.disabled).toBe(true);
    const offered = Array.from(picker.options)
      .map((option) => option.value)
      .filter(Boolean);
    expect(offered.sort()).toEqual([F_MASTER, F_LIVE].sort());

    fireEvent.click(screen.getByRole("checkbox", { name: "Select live" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Select lv" }));
    expect(mapButton.disabled).toBe(true);
    fireEvent.change(picker, { target: { value: F_LIVE } });
    expect(mapButton.disabled).toBe(false);
    fireEvent.click(mapButton);

    await waitFor(() =>
      expect(writes()).toEqual([
        [ENTRIES, "POST", { version_texts: ["live", "lv"], file_id: F_LIVE }],
      ])
    );
  });

  it("maps a title-words text too (a venue the log reader doesn't know)", async () => {
    renderPanel([
      LIVE,
      version("woodstock 99", { version_type: "unknown", reads_as_version: false }),
    ]);

    fireEvent.click(screen.getByRole("checkbox", { name: "Select woodstock 99" }));
    fireEvent.change(screen.getByRole("combobox", { name: "Map to file" }), {
      target: { value: F_LIVE },
    });
    fireEvent.click(screen.getByRole("button", { name: "Map selected" }));

    await waitFor(() =>
      expect(writes()).toEqual([
        [ENTRIES, "POST", { version_texts: ["woodstock 99"], file_id: F_LIVE }],
      ])
    );
  });

  it("offers a dismissed text for mapping, but not a mapped one", () => {
    renderPanel([MAPPED, DISMISSED]);

    expect(screen.getByRole("checkbox", { name: "Select acoustic" })).toBeTruthy();
    expect(screen.queryByRole("checkbox", { name: "Select unplugged" })).toBeNull();
  });

  it("unmaps a mapped text", async () => {
    renderPanel([MAPPED]);

    fireEvent.click(screen.getByRole("button", { name: "Unmap unplugged" }));

    await waitFor(() =>
      expect(writes()).toEqual([[ENTRIES, "DELETE", { version_texts: ["unplugged"] }]])
    );
  });

  it("dismisses an unmapped text; a mapped or dismissed one offers no Dismiss", async () => {
    renderPanel([DEMO, MAPPED, DISMISSED]);

    expect(screen.queryByRole("button", { name: "Dismiss unplugged" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Dismiss acoustic" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Dismiss demo" }));

    await waitFor(() => expect(writes()).toEqual([[DISMISS, "POST", { version_texts: ["demo"] }]]));
  });

  it("waits while a write is in flight", async () => {
    let finish: () => void = () => {};
    mockedApiFetch.mockImplementation(
      () =>
        new Promise<never>((resolve) => {
          finish = () => resolve(undefined as never);
        })
    );
    renderPanel([LIVE, DEMO]);

    fireEvent.click(screen.getByRole("button", { name: "Dismiss demo" }));

    await waitFor(() =>
      expect(
        (screen.getByRole("button", { name: "Dismiss live" }) as HTMLButtonElement).disabled
      ).toBe(true)
    );
    fireEvent.click(screen.getByRole("button", { name: "Dismiss live" }));
    expect(writes()).toHaveLength(1);
    finish();
    await waitFor(() =>
      expect(
        (screen.getByRole("button", { name: "Dismiss live" }) as HTMLButtonElement).disabled
      ).toBe(false)
    );
  });

  it("says why a write was refused", async () => {
    mockedApiFetch.mockRejectedValue(new ConflictError("Version 'live' is mapped; unmap it first"));
    renderPanel([LIVE]);

    fireEvent.click(screen.getByRole("button", { name: "Dismiss live" }));

    expect((await screen.findByRole("alert")).textContent).toContain(
      "Version 'live' is mapped; unmap it first"
    );
  });
});

describe("WorkVersionsPanel: suggestions and the master flag", () => {
  const SUGGESTED = version("live", {
    suggestion: {
      file_id: F_LIVE,
      reason: "only_copy",
      track_title: "Enter Sandman (Live at Seattle)",
      file_path: `D:/music/${F_LIVE}.flac`,
    },
  });

  it("shows a suggestion and maps it only when pressed (C64)", async () => {
    renderPanel([SUGGESTED]);

    const row = screen.getByRole("listitem", { name: "live" });
    expect(row.textContent).toContain("Suggested");
    expect(row.textContent).toContain("Enter Sandman (Live at Seattle)");
    expect(writes()).toEqual([]);

    fireEvent.click(within(row).getByRole("button", { name: "Use suggestion for live" }));

    await waitFor(() =>
      expect(writes()).toEqual([[ENTRIES, "POST", { version_texts: ["live"], file_id: F_LIVE }]])
    );
  });

  it("flags a live master when the work holds a plain copy (C66)", () => {
    renderPanel([], { master_file_id: F_LIVE, version_type: "live", plain_file_ids: [F_MASTER] });

    const flag = screen.getByText(/plain copy/i);
    expect(flag.textContent).toMatch(/master/i);
    expect(flag.textContent).toContain("live");
  });

  it("shows no flag without one", () => {
    renderPanel([LIVE]);

    expect(screen.queryByText(/plain copy/i)).toBeNull();
  });
});

describe("the work page", () => {
  const ARTIST = "a-metallica";

  function serveWork(detail: Record<string, unknown>) {
    mockedApiFetch.mockImplementation((url: string) => {
      if (url === `/api/v1/library/works/${WORK}`) return Promise.resolve(detail);
      if (url === `/api/v1/library/artists/${ARTIST}`)
        return Promise.resolve({
          id: ARTIST,
          name: "Metallica",
          sort_name: "Metallica",
          disambiguation: null,
          works: [],
          merged_into: null,
          members: [],
        });
      return Promise.reject(new ApiError(404, "not found"));
    });
  }

  const DETAIL = {
    id: WORK,
    title: "Enter Sandman",
    artist_id: ARTIST,
    recordings: RECORDINGS,
    song_master: null,
    format_overrides: [],
  };

  function renderPage() {
    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={[`/library/artists/${ARTIST}/works/${WORK}`]}>
          <Routes>
            <Route
              path="/library/artists/:artist_id/works/:work_id"
              element={<AssociatedWorks />}
            />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    );
  }

  it("shows the work's versions beside the format overrides, from the work detail", async () => {
    serveWork({ ...DETAIL, versions: [LIVE], master_flag: null });
    renderPage();

    expect(await screen.findByRole("listitem", { name: "live" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Format Overrides", level: 3 })).toBeTruthy();
  });

  it("shows an empty list for a work detail without versions", async () => {
    serveWork(DETAIL);
    renderPage();

    expect(await screen.findByText(/No version texts/)).toBeTruthy();
  });
});
