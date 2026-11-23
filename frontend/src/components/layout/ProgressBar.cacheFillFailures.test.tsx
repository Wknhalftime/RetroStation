// @vitest-environment jsdom
// Phase 4.1 PR 2b (SPEC S11, D10): a fill run shows its failed calls. While it runs, the detail
// its batch sends counts them ("payoff, 37 of 100 (cap 2,600), 12 failed"); as it finishes, the
// bar's amber warning beside "Done" reads "652 calls failed: a later run asks them again". A run
// with no failed call finishes as before, with no warning.
import { describe, it, expect, afterEach } from "vitest";
import { act, cleanup, render } from "@testing-library/react";
import { useProgressStore } from "@/store/progressStore";
import type { TaskInfo } from "@/lib/schemas/tasks";
import { ProgressBar } from "./ProgressBar";

function fillRow(status: string, data: Record<string, unknown>): TaskInfo {
  return {
    task_id: "mb_cache_fill-1",
    task_type: "mb_cache_fill",
    status,
    progress_data: data,
    started_at: "2026-11-23T09:00:00Z",
    updated_at: "2026-11-23T09:01:00Z",
    completed_at: status === "completed" ? "2026-11-23T09:02:00Z" : null,
  };
}

function finished(data: Record<string, unknown>): string {
  act(() => useProgressStore.getState().setTasks([fillRow("running", { processed: 1 })]));
  act(() => useProgressStore.getState().setTasks([fillRow("completed", data)]));
  return render(<ProgressBar />).container.textContent ?? "";
}

afterEach(() => {
  cleanup();
  act(() => useProgressStore.getState().setTasks([]));
});

describe("ProgressBar: a cache fill's failed calls", () => {
  it("D10: a running batch's detail counts the failed calls", () => {
    act(() =>
      useProgressStore.getState().setTasks([
        fillRow("running", {
          processed: 37,
          total: 100,
          detail: "payoff, 37 of 100 (cap 2,600), 12 failed",
        }),
      ])
    );
    const text = render(<ProgressBar />).container.textContent ?? "";
    expect(text).toContain(
      "Filling the MusicBrainz cache: payoff, 37 of 100 (cap 2,600), 12 failed"
    );
  });

  it("D10: a finished run with failed calls says so in its warning", () => {
    const text = finished({
      detail: "aliases, 18 of 18",
      ended: "done",
      warning: "mb_fill_calls_failed",
      failed_calls: 652,
    });
    expect(text).toContain("Filling the MusicBrainz cache: aliases, 18 of 18 — Done");
    expect(text).toContain("652 calls failed: a later run asks them again");
  });

  it("D8, D10: a cut-off run keeps D8's words, and its warning counts the failures", () => {
    const text = finished({
      detail: "payoff, stopped (MusicBrainz not answering)",
      cut_off: true,
      ended: "cut_off",
      warning: "mb_fill_calls_failed",
      failed_calls: 5,
    });
    expect(text).toContain(
      "Filling the MusicBrainz cache: payoff, stopped (MusicBrainz not answering)"
    );
    expect(text).toContain("5 calls failed: a later run asks them again");
  });

  it("a run with no failed call finishes with no warning", () => {
    const text = finished({ detail: "aliases, 18 of 18", ended: "done" });
    expect(text).toContain("Filling the MusicBrainz cache: aliases, 18 of 18 — Done");
    expect(text).not.toContain("failed");
  });
});
