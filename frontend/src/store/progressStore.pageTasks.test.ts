// @vitest-environment jsdom
// PR G2, Task 9 (traceability VH1-VH5; rows D89, D90, I8, D77a): the progress store and the
// app-wide bottom bar. D90: the cost meter's one "stream_resources" row is a meter, not a
// task: in no status (running, completed, failed) does it light the bottom bar, count as
// running, count in "+N more", or become the active task; only the page sees it (I8). D89:
// a cue run ("cue_analysis") shows in the bottom bar like scans and enrichment, with a label
// of its own (not the raw task type) and its percent. Other tasks behave as before.
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { createElement } from "react";
import { act, cleanup, render } from "@testing-library/react";
import { useProgressStore } from "./progressStore";
import { ProgressBar } from "@/components/layout/ProgressBar";
import type { TaskInfo } from "@/lib/schemas/tasks";

type Status = "running" | "completed" | "failed";

function task(
  id: string,
  type: string,
  status: Status,
  progressData: Record<string, unknown> = {},
  startedAt = "2026-10-02T09:00:00Z"
): TaskInfo {
  return {
    task_id: id,
    task_type: type,
    status,
    progress_data: progressData,
    started_at: startedAt,
    updated_at: "2026-10-02T09:05:00Z",
    completed_at: status === "running" ? null : "2026-10-02T09:05:00Z",
  };
}

function meter(status: Status): TaskInfo {
  // Started after the scan, so a store that ranks it would pick it as the active task.
  return task(
    "stream_resources",
    "stream_resources",
    status,
    { open_streams: 1, scope: "audio_engines", suggested_max: 57 },
    "2026-10-02T09:30:00Z"
  );
}

function setTasks(tasks: TaskInfo[]) {
  act(() => useProgressStore.getState().setTasks(tasks));
}

function expectBarDark() {
  const state = useProgressStore.getState();
  expect(state.status).toBe("IDLE");
  expect(state.activeTask).toBeNull();
  expect(state.extraCount).toBe(0);
  expect(state.visibleTasks).toEqual([]);
  expect(state.runningTasks).toEqual([]);
  expect(state.terminalTaskIds).toEqual([]);
  expect(state.hasRunningType("stream_resources")).toBe(false);
  const view = render(createElement(ProgressBar));
  expect(view.container.textContent ?? "").toBe("");
  view.unmount();
}

beforeEach(() => {
  vi.useFakeTimers();
  setTasks([]);
});

afterEach(() => {
  cleanup();
  setTasks([]);
  vi.useRealTimers();
});

describe("progress store: page-only rows", () => {
  it("VH1: a meter row never lights the bottom bar [running, completed, failed]", () => {
    for (const status of ["running", "completed", "failed"] as const) {
      setTasks([]);
      setTasks([meter("running")]);
      expectBarDark();
      setTasks([meter(status)]);
      expectBarDark();
    }
  });

  it("VH2: the page sees the meter row", () => {
    const scan = task("scan-1", "scan", "running");
    setTasks([scan, meter("running")]);
    expect(useProgressStore.getState().pageTasks).toEqual([meter("running")]);
    setTasks([scan]);
    expect(useProgressStore.getState().pageTasks).toEqual([]);
  });

  it("VH3: a cue run shows in the bottom bar with its label", () => {
    const run = task("cue_analysis-1", "cue_analysis", "running", { processed: 40, total: 100 });
    setTasks([run]);
    const state = useProgressStore.getState();
    expect(state.status).toBe("RUNNING");
    expect(state.activeTask?.task_id).toBe("cue_analysis-1");
    expect(state.hasRunningType("cue_analysis")).toBe(true);
    const view = render(createElement(ProgressBar));
    const text = view.container.textContent ?? "";
    expect(text).toMatch(/40\s*%/);
    expect(text).not.toMatch(/cue_analysis/);
    expect(text).toMatch(/cue|measur|song|track|stream|analy/i);
  });

  it("VH4: other tasks behave as before", () => {
    const scan = task("scan-1", "scan", "running", { processed: 1, total: 4 });
    setTasks([scan]);
    let state = useProgressStore.getState();
    expect(state.status).toBe("RUNNING");
    expect(state.runningTasks).toEqual([scan]);
    expect(state.pageTasks).toEqual([]);
    expect(state.hasRunningType("scan")).toBe(true);
    setTasks([task("scan-1", "scan", "completed")]);
    state = useProgressStore.getState();
    expect(state.status).toBe("COMPLETED");
    expect(state.activeTask?.task_id).toBe("scan-1");
    setTasks([task("enrich-1", "library_enrichment", "failed", { error: "boom" })]);
    expect(useProgressStore.getState().status).toBe("FAILED");
  });

  it("VH5: a scan beside the meter: the scan is active and extraCount is 0", () => {
    const scan = task("scan-1", "scan", "running", { processed: 1, total: 4 });
    setTasks([scan, meter("running")]);
    const state = useProgressStore.getState();
    expect(state.status).toBe("RUNNING");
    expect(state.activeTask?.task_id).toBe("scan-1");
    expect(state.extraCount).toBe(0);
    expect(state.visibleTasks).toEqual([scan]);
    expect(state.runningTasks).toEqual([scan]);
    const view = render(createElement(ProgressBar));
    expect(view.container.textContent ?? "").not.toMatch(/stream_resources/);
  });
});
