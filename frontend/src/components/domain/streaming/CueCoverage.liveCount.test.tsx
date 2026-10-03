// @vitest-environment jsdom
// PR G2 final review M4 and M6 (not a locked test).
// M4: while a cue run is live, "X of Y cues ready" follows the run's progress row (processed of
// total), so it does not go stale between the page's one count and the run's end; it never
// goes back below the count the page read (the two only grow during a run), and the run's end
// still re-reads the real counts (VG3).
// M6: the live run is labelled "Measuring songs for streaming", as in the bottom bar.
import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, render, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

vi.mock("@/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/api/client")>();
  return { ...actual, apiFetch: vi.fn(), apiUpload: vi.fn() };
});

import { apiFetch } from "@/api/client";
import { useProgressStore } from "@/store/progressStore";
import type { TaskInfo } from "@/lib/schemas/tasks";
import { CueCoverage } from "./CueCoverage";

const mockedApiFetch = vi.mocked(apiFetch);
const COVERAGE_URL = "/api/v1/streaming/cue-coverage";

function cueRun(processed: number, total: number): TaskInfo {
  return {
    task_id: "cue-run-1",
    task_type: "cue_analysis",
    status: "running",
    progress_data: { processed, total },
    started_at: "2026-10-03T09:00:00Z",
    updated_at: "2026-10-03T09:01:00Z",
    completed_at: null,
  };
}

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

function countLine(container: HTMLElement): string {
  const line = Array.from(container.querySelectorAll("p")).find((p) =>
    /cues ready/i.test(p.textContent ?? "")
  );
  return line?.textContent ?? "";
}

beforeEach(() => {
  mockedApiFetch.mockReset();
  mockedApiFetch.mockImplementation((url: string) =>
    url === COVERAGE_URL
      ? Promise.resolve({ analysable: 500, ready: 120, failed: 5, unhashed: 7 })
      : Promise.reject(new Error(`unexpected ${url}`))
  );
  act(() => useProgressStore.getState().setTasks([]));
});

describe("CueCoverage while a run is live", () => {
  it("M4: X of Y follows the run's row", async () => {
    const view = render(<CueCoverage />, { wrapper });
    await waitFor(() => expect(countLine(view.container)).toMatch(/^125 of 500 cues ready/));
    act(() => useProgressStore.getState().setTasks([cueRun(300, 520)]));
    expect(countLine(view.container)).toMatch(/^300 of 520 cues ready/);
    act(() => useProgressStore.getState().setTasks([cueRun(310, 520)]));
    expect(countLine(view.container)).toMatch(/^310 of 520 cues ready/);
  });

  it("M4: X of Y never drops below the count the page read", async () => {
    const view = render(<CueCoverage />, { wrapper });
    await waitFor(() => expect(countLine(view.container)).toMatch(/^125 of 500 cues ready/));
    act(() => useProgressStore.getState().setTasks([cueRun(100, 480)]));
    expect(countLine(view.container)).toMatch(/^125 of 500 cues ready/);
  });

  it("M6: the live run is labelled Measuring songs for streaming", async () => {
    const view = render(<CueCoverage />, { wrapper });
    await waitFor(() => expect(countLine(view.container)).toMatch(/cues ready/));
    act(() => useProgressStore.getState().setTasks([cueRun(300, 520)]));
    expect(view.container.textContent ?? "").toMatch(/Measuring songs for streaming/);
    expect(view.container.textContent ?? "").not.toMatch(/Analysing cue points/);
  });
});
