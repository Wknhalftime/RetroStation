// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

vi.mock("@/api/client", () => ({ apiFetch: vi.fn() }));

import { apiFetch } from "@/api/client";
import { SilentFiles } from "./SilentFiles";

const mockedApiFetch = vi.mocked(apiFetch);
const URL = "/api/v1/library/silent-files";
const BLANK = {
  file_id: "11111111-1111-4111-8111-111111111111",
  artist: "No Artist",
  title: "Blank",
  album: "HitDisc 883B",
  path: "D:\\Music\\rips\\track01.flac",
  duration_ms: 6000,
  reasons: ["silent"],
  bitrate_kbps: 1,
  cued_span_ms: null,
};
const TESTIMONY = {
  file_id: "22222222-2222-4222-8222-222222222222",
  artist: "Mary J. Blige",
  title: "Testimony",
  album: "No More Drama",
  path: "D:\\Music\\rips\\track07.flac",
  duration_ms: 298666,
  reasons: ["low_bitrate", "short_cued_span"],
  bitrate_kbps: 17,
  cued_span_ms: 11700,
};
const LONG_SILENCE = {
  file_id: "33333333-3333-4333-8333-333333333333",
  artist: "Some Band",
  title: "Hidden Silence",
  album: "Some Album",
  path: "D:\\Music\\rips\\track12.flac",
  duration_ms: 120000,
  reasons: ["silent", "low_bitrate"],
  bitrate_kbps: 2,
  cued_span_ms: null,
};

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

function serve(body: unknown) {
  mockedApiFetch.mockImplementation((url: string) =>
    Promise.resolve(url === URL ? body : undefined)
  );
}

async function rowText(title: string): Promise<string> {
  const row = (await screen.findByText(title)).closest("tr");
  if (row === null) throw new Error(`no row for ${title}`);
  return row.textContent ?? "";
}

beforeEach(() => {
  mockedApiFetch.mockReset();
});

describe("silent files page", () => {
  it("lists each file with its artist, title, album, path, duration and why", async () => {
    serve({ files: [BLANK, LONG_SILENCE, TESTIMONY], silent: 2, nearly_silent: 1 });
    render(<SilentFiles />, { wrapper });

    const testimony = await rowText("Testimony");
    for (const part of [
      "Mary J. Blige",
      "No More Drama",
      TESTIMONY.path,
      "4:58",
      "Low bitrate: 17 kb/s",
      "Audible 0:11 of 4:58",
    ])
      expect(testimony).toContain(part);
    const blank = await rowText("Blank");
    for (const part of ["No Artist", "HitDisc 883B", BLANK.path, "0:06"])
      expect(blank).toContain(part);
    expect(blank).toContain("Silent: no audio (skipped on air)");
    const both = await rowText("Hidden Silence");
    expect(both).toContain("Silent: no audio (skipped on air)");
    expect(both).toContain("Low bitrate: 2 kb/s");
    expect(screen.getAllByRole("row")).toHaveLength(4); // the heading row and one per file
    for (const heading of ["Artist", "Title", "Album", "Path", "Duration"])
      expect(screen.getByRole("columnheader", { name: heading })).toBeTruthy();
    expect(screen.queryByText("No silent or nearly silent files found.")).toBeNull();
    expect(screen.getByText(/2 silent, 1 nearly silent/)).toBeTruthy();
    expect(mockedApiFetch).toHaveBeenCalledWith(URL);
  });

  it("says so when no file is silent", async () => {
    serve({ files: [], silent: 0, nearly_silent: 0 });
    render(<SilentFiles />, { wrapper });
    expect(await screen.findByText("No silent or nearly silent files found.")).toBeTruthy();
  });

  it("says when the report cannot be loaded", async () => {
    mockedApiFetch.mockRejectedValue(new Error("unavailable"));
    render(<SilentFiles />, { wrapper });
    expect(await screen.findByText("Failed to load.")).toBeTruthy();
  });
});
