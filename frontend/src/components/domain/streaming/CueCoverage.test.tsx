// @vitest-environment jsdom
// PR G2, Task 9 (traceability VG1-VG3; rows D77, D89, PG7, S10): "cues ready X of Y" on the
// Streaming page. D77/PG7: the counts come from GET /api/v1/streaming/cue-coverage. D89: while
// a cue run is live its progress shows on the page too, from the same progress row the bottom
// bar shows (D77a: through the progress store, not a new mechanism); when the run completes
// the page re-reads the counts. Ruling D89a: X is the settled audio, ready plus failed (the
// same count the bar's row carries, T7.8), and the failed count is shown separately.
// No G2 copy here is spec-named: the count is checked by its numbers in order, and the live
// bar by its role and value.
import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, render, screen, waitFor } from "@testing-library/react";
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

let served = { analysable: 500, ready: 120, failed: 0, unhashed: 7 };

function cueRun(status: "running" | "completed", processed = 40, total = 100): TaskInfo {
  return {
    task_id: "cue_analysis-run-1",
    task_type: "cue_analysis",
    status,
    progress_data: { processed, total },
    started_at: "2026-10-02T09:00:00Z",
    updated_at: "2026-10-02T09:01:00Z",
    completed_at: status === "running" ? null : "2026-10-02T09:02:00Z",
  };
}

function meterRow(): TaskInfo {
  return {
    task_id: "stream_resources",
    task_type: "stream_resources",
    status: "running",
    progress_data: { open_streams: 0, scope: "audio_engines" },
    started_at: "2026-10-02T08:00:00Z",
    updated_at: "2026-10-02T09:01:00Z",
    completed_at: null,
  };
}

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

function coverageReads() {
  return mockedApiFetch.mock.calls.filter(([url]) => url === COVERAGE_URL).length;
}

/** "X of Y": X (ready + failed, ruling D89a) then ``analysable``, in that order, in the
 * section's text, as whole numbers (no digit beside them: a heading and a figure run together
 * in the text). */
function readyOf(settled: number, analysable: number): RegExp {
  return new RegExp(`(?<!\\d)${settled}(?!\\d)[\\s\\S]*(?<!\\d)${analysable}(?!\\d)`);
}

beforeEach(() => {
  served = { analysable: 500, ready: 120, failed: 5, unhashed: 7 };
  mockedApiFetch.mockReset();
  mockedApiFetch.mockImplementation((url: string) =>
    url === COVERAGE_URL
      ? Promise.resolve({ ...served })
      : Promise.reject(new Error(`unexpected ${url}`))
  );
  act(() => useProgressStore.getState().setTasks([]));
});

describe("CueCoverage", () => {
  it("VG1: shows cues ready X of Y", async () => {
    const view = render(<CueCoverage />, { wrapper });
    // 120 ready + 5 failed = 125 settled of 500; the 5 failed are shown on their own too.
    await waitFor(() => expect(view.container.textContent ?? "").toMatch(readyOf(125, 500)));
    expect(view.container.textContent ?? "").toMatch(/(?<!\d)(?<!\d\.)5(?!\.?\d)/);
    expect(screen.queryByRole("progressbar")).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("VG2: while a cue run is live, shows its bar on the page too", async () => {
    const view = render(<CueCoverage />, { wrapper });
    await waitFor(() => expect(view.container.textContent ?? "").toMatch(readyOf(125, 500)));
    act(() => useProgressStore.getState().setTasks([meterRow()]));
    expect(screen.queryByRole("progressbar")).toBeNull();
    act(() => useProgressStore.getState().setTasks([cueRun("running", 40, 100), meterRow()]));
    const bar = await screen.findByRole("progressbar");
    expect(bar.getAttribute("aria-valuenow")).toBe("40");
  });

  it("VG3: re-reads the counts when the run completes", async () => {
    act(() => useProgressStore.getState().setTasks([cueRun("running", 120, 500)]));
    const view = render(<CueCoverage />, { wrapper });
    await waitFor(() => expect(view.container.textContent ?? "").toMatch(readyOf(125, 500)));
    const before = coverageReads();
    served = { analysable: 500, ready: 180, failed: 7, unhashed: 7 };
    act(() => useProgressStore.getState().setTasks([cueRun("completed", 187, 500)]));
    await waitFor(() => expect(view.container.textContent ?? "").toMatch(readyOf(187, 500)));
    expect(coverageReads()).toBeGreaterThan(before);
  });
});
