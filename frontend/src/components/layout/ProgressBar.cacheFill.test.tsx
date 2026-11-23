// @vitest-environment jsdom
// Phase 4.1 PR 2 (SPEC D4, M10): the MusicBrainz cache fill shows in the bottom bar as "Filling
// the MusicBrainz cache", followed by the detail its row sends ("payoff, 37 of 100 (cap 2,600)").
// Any task's row may send a detail; the bar puts it after the label, running or finished, and a
// row with none shows its label alone. A run the cut-off stopped says so on its finished row
// (D8), so its green Done never hides an outage. The names re-check the fill hands off (S7) has
// a label of its own (D7).
import { describe, it, expect, afterEach } from "vitest";
import { act, cleanup, render } from "@testing-library/react";
import { useProgressStore } from "@/store/progressStore";
import type { TaskInfo } from "@/lib/schemas/tasks";
import { ProgressBar } from "./ProgressBar";

function running(type: string, data: Record<string, unknown>): TaskInfo {
  return {
    task_id: `${type}-1`,
    task_type: type,
    status: "running",
    progress_data: data,
    started_at: "2026-11-22T09:00:00Z",
    updated_at: "2026-11-22T09:01:00Z",
    completed_at: null,
  };
}

function shown(task: TaskInfo): string {
  act(() => useProgressStore.getState().setTasks([task]));
  return render(<ProgressBar />).container.textContent ?? "";
}

afterEach(() => {
  cleanup();
  act(() => useProgressStore.getState().setTasks([]));
});

describe("ProgressBar: the MusicBrainz cache fill", () => {
  it("D4: a running fill reads its label, then its tier, batch and cap", () => {
    const text = shown(
      running("mb_cache_fill", {
        tier: "payoff",
        processed: 37,
        total: 100,
        cap: 2600,
        detail: "payoff, 37 of 100 (cap 2,600)",
      })
    );
    expect(text).toContain("Filling the MusicBrainz cache: payoff, 37 of 100 (cap 2,600)");
  });

  it("M10: any task's detail follows its label", () => {
    const text = shown(running("scan", { processed: 3, total: 9, detail: "folder 3 of 9" }));
    expect(text).toContain("Scanning library: folder 3 of 9");
  });

  it("M10: a task with no detail shows its label alone", () => {
    const text = shown(running("scan", { processed: 3, total: 9 }));
    expect(text).toContain("Scanning library");
    expect(text).not.toContain("Scanning library:");
  });

  it("D8: a run the cut-off stopped says so as it finishes", () => {
    const fill = running("mb_cache_fill", { tier: "payoff", processed: 4, total: 100 });
    act(() => useProgressStore.getState().setTasks([fill]));
    act(() =>
      useProgressStore.getState().setTasks([
        {
          ...fill,
          status: "completed",
          completed_at: "2026-11-22T09:02:00Z",
          progress_data: {
            tier: "payoff",
            cut_off: true,
            detail: "payoff, stopped (MusicBrainz not answering)",
          },
        },
      ])
    );
    const text = render(<ProgressBar />).container.textContent ?? "";
    expect(text).toContain(
      "Filling the MusicBrainz cache: payoff, stopped (MusicBrainz not answering)"
    );
  });

  it("D7: the names re-check reads as its own step", () => {
    const text = shown(running("names_recheck", { processed: 0, total: 1 }));
    expect(text).toContain("Re-checking the names the cache fill searched");
  });
});
