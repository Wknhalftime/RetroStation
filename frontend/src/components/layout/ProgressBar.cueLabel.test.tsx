// @vitest-environment jsdom
// PR G2 final review M6 (not a locked test): a cue run shows in the bottom bar as "Measuring
// songs for streaming" (plan manual check 6), the same words the Streaming page uses; a failed
// run shows the reason its row carries (M7).
import { describe, it, expect, afterEach } from "vitest";
import { act, cleanup, render } from "@testing-library/react";
import { useProgressStore } from "@/store/progressStore";
import type { TaskInfo } from "@/lib/schemas/tasks";
import { ProgressBar } from "./ProgressBar";

function cueRun(status: "running" | "failed", data: Record<string, unknown>): TaskInfo {
  return {
    task_id: "cue-run-1",
    task_type: "cue_analysis",
    status,
    progress_data: data,
    started_at: "2026-10-03T09:00:00Z",
    updated_at: "2026-10-03T09:01:00Z",
    completed_at: status === "running" ? null : "2026-10-03T09:01:00Z",
  };
}

afterEach(() => {
  cleanup();
  act(() => useProgressStore.getState().setTasks([]));
});

describe("ProgressBar: the cue run", () => {
  it("M6: a running cue run reads Measuring songs for streaming", () => {
    act(() =>
      useProgressStore.getState().setTasks([cueRun("running", { processed: 40, total: 100 })])
    );
    const view = render(<ProgressBar />);
    expect(view.container.textContent ?? "").toMatch(/Measuring songs for streaming/);
  });

  it("M7: a failed cue run shows its reason", () => {
    const reason = "the cue run stopped on an error; System Logs has the details";
    act(() =>
      useProgressStore
        .getState()
        .setTasks([cueRun("failed", { processed: 4, total: 9, error: reason })])
    );
    const view = render(<ProgressBar />);
    expect(view.container.textContent ?? "").toContain(reason);
  });
});
