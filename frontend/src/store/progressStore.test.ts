import { beforeEach, afterEach, describe, it, expect, vi } from "vitest";
import { useProgressStore } from "./progressStore";
import type { TaskInfo } from "@/lib/schemas/tasks";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makeTask(
  id: string,
  status: "running" | "completed" | "failed",
  overrides: Partial<TaskInfo> = {},
): TaskInfo {
  return {
    task_id: id,
    task_type: "scan",
    status,
    progress_data: {},
    started_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    completed_at: status !== "running" ? "2026-01-01T00:00:01Z" : null,
    ...overrides,
  };
}

const IDLE_STATE = {
  status: "IDLE" as const,
  activeTask: null,
  extraCount: 0,
  visibleTasks: [],
  runningTasks: [],
  terminalTaskIds: [],
  dismissTimer: null,
  dismissedTaskIds: [],
};

// ---------------------------------------------------------------------------
// Setup
// ---------------------------------------------------------------------------

beforeEach(() => {
  // Merge-reset data fields only — passing `true` would wipe the action
  // functions from the store, which live alongside state in Zustand v5.
  useProgressStore.setState(IDLE_STATE);
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

// ---------------------------------------------------------------------------
// setTasks — running branch
// ---------------------------------------------------------------------------

describe("setTasks — RUNNING", () => {
  it("transitions to RUNNING when at least one running task is present", () => {
    useProgressStore.getState().setTasks([makeTask("t1", "running")]);
    expect(useProgressStore.getState().status).toBe("RUNNING");
  });

  it("visibleTasks contains all tasks including grace-window terminal rows", () => {
    const running = makeTask("t1", "running");
    const grace = makeTask("t2", "failed");
    useProgressStore.getState().setTasks([running, grace]);
    const { visibleTasks } = useProgressStore.getState();
    expect(visibleTasks.map((t) => t.task_id)).toEqual(["t1", "t2"]);
  });

  it("runningTasks contains only in-flight tasks, never terminal rows", () => {
    useProgressStore.getState().setTasks([
      makeTask("t1", "running"),
      makeTask("t2", "failed"),
    ]);
    const { runningTasks } = useProgressStore.getState();
    expect(runningTasks.map((t) => t.task_id)).toEqual(["t1"]);
  });

  it("terminalTaskIds tracks grace-window terminal IDs for dismiss suppression", () => {
    useProgressStore.getState().setTasks([
      makeTask("t1", "running"),
      makeTask("t2", "failed"),
      makeTask("t3", "completed"),
    ]);
    expect(useProgressStore.getState().terminalTaskIds.sort()).toEqual(["t2", "t3"]);
  });

  it("terminalTaskIds is empty when no terminal rows are in the payload", () => {
    useProgressStore.getState().setTasks([makeTask("t1", "running")]);
    expect(useProgressStore.getState().terminalTaskIds).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// setTasks — FAILED branch
// ---------------------------------------------------------------------------

describe("setTasks — FAILED", () => {
  it("transitions to FAILED with no running tasks", () => {
    useProgressStore.getState().setTasks([makeTask("t1", "failed")]);
    expect(useProgressStore.getState().status).toBe("FAILED");
  });

  it("terminalTaskIds holds all failed task IDs so dismiss() can suppress them", () => {
    useProgressStore.getState().setTasks([
      makeTask("t1", "failed"),
      makeTask("t2", "failed"),
    ]);
    expect(useProgressStore.getState().terminalTaskIds.sort()).toEqual(["t1", "t2"]);
  });

  it("visibleTasks is empty — rendering uses activeTask, not visibleTasks", () => {
    useProgressStore.getState().setTasks([makeTask("t1", "failed")]);
    expect(useProgressStore.getState().visibleTasks).toEqual([]);
  });

  it("extraCount reflects number of additional failed tasks beyond the active one", () => {
    useProgressStore.getState().setTasks([
      makeTask("t1", "failed"),
      makeTask("t2", "failed"),
      makeTask("t3", "failed"),
    ]);
    expect(useProgressStore.getState().extraCount).toBe(2);
  });
});


// ---------------------------------------------------------------------------
// setTasks — COMPLETED branch
// ---------------------------------------------------------------------------

describe("setTasks — COMPLETED", () => {
  it("transitions from RUNNING to COMPLETED when only completed tasks remain", () => {
    useProgressStore.getState().setTasks([makeTask("t1", "running")]);
    useProgressStore.getState().setTasks([makeTask("t1", "completed")]);
    expect(useProgressStore.getState().status).toBe("COMPLETED");
  });

  it("terminalTaskIds holds completed task IDs", () => {
    useProgressStore.getState().setTasks([makeTask("t1", "running")]);
    useProgressStore.getState().setTasks([makeTask("t1", "completed")]);
    expect(useProgressStore.getState().terminalTaskIds).toEqual(["t1"]);
  });

  it("auto-dismisses to IDLE after 2 seconds", () => {
    useProgressStore.getState().setTasks([makeTask("t1", "running")]);
    useProgressStore.getState().setTasks([makeTask("t1", "completed")]);
    vi.advanceTimersByTime(2000);
    expect(useProgressStore.getState().status).toBe("IDLE");
    expect(useProgressStore.getState().terminalTaskIds).toEqual([]);
  });

  it("ignores completed tasks when status is not RUNNING (no spurious transitions)", () => {
    useProgressStore.getState().setTasks([makeTask("t1", "completed")]);
    expect(useProgressStore.getState().status).toBe("IDLE");
  });
});

// ---------------------------------------------------------------------------
// setTasks — IDLE transitions
// ---------------------------------------------------------------------------

describe("setTasks — transitioning to IDLE", () => {
  it("moves from RUNNING to IDLE when the payload becomes empty", () => {
    useProgressStore.getState().setTasks([makeTask("t1", "running")]);
    useProgressStore.getState().setTasks([]);
    expect(useProgressStore.getState().status).toBe("IDLE");
  });

  it("clears all fields when transitioning to IDLE", () => {
    useProgressStore.getState().setTasks([makeTask("t1", "running")]);
    useProgressStore.getState().setTasks([]);
    const s = useProgressStore.getState();
    expect(s.activeTask).toBeNull();
    expect(s.visibleTasks).toEqual([]);
    expect(s.runningTasks).toEqual([]);
    expect(s.terminalTaskIds).toEqual([]);
    expect(s.dismissedTaskIds).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// dismiss() — core fix
// ---------------------------------------------------------------------------

describe("dismiss()", () => {
  it("moves status to IDLE and records terminalTaskIds into dismissedTaskIds", () => {
    useProgressStore.getState().setTasks([makeTask("t1", "failed")]);
    useProgressStore.getState().dismiss();
    const s = useProgressStore.getState();
    expect(s.status).toBe("IDLE");
    expect(s.dismissedTaskIds).toEqual(["t1"]);
    expect(s.terminalTaskIds).toEqual([]);
  });

  it("next WS tick with the same failed tasks does not restore FAILED status", () => {
    useProgressStore.getState().setTasks([makeTask("t1", "failed")]);
    useProgressStore.getState().dismiss();
    useProgressStore.getState().setTasks([makeTask("t1", "failed")]);
    expect(useProgressStore.getState().status).toBe("IDLE");
  });

  it("suppresses all failed tasks when multiple were shown at dismiss time", () => {
    useProgressStore.getState().setTasks([
      makeTask("t1", "failed"),
      makeTask("t2", "failed"),
    ]);
    useProgressStore.getState().dismiss();
    useProgressStore.getState().setTasks([
      makeTask("t1", "failed"),
      makeTask("t2", "failed"),
    ]);
    expect(useProgressStore.getState().status).toBe("IDLE");
  });
});

// ---------------------------------------------------------------------------
// dismissedTaskIds pruning (unexpiredDismissedIds)
// ---------------------------------------------------------------------------

describe("dismissedTaskIds pruning", () => {
  it("removes IDs that have dropped out of the WS payload when the grace window expires", () => {
    useProgressStore.getState().setTasks([makeTask("t1", "failed")]);
    useProgressStore.getState().dismiss();
    useProgressStore.getState().setTasks([]);
    expect(useProgressStore.getState().dismissedTaskIds).toEqual([]);
  });

  it("retains IDs still present in the payload and drops only those that have expired", () => {
    useProgressStore.getState().setTasks([
      makeTask("t1", "failed"),
      makeTask("t2", "failed"),
    ]);
    useProgressStore.getState().dismiss();
    // t2 has left the payload; t1 is still within the grace window.
    useProgressStore.getState().setTasks([makeTask("t1", "failed")]);
    expect(useProgressStore.getState().dismissedTaskIds).toEqual(["t1"]);
  });
});

// ---------------------------------------------------------------------------
// hasRunningType()
// ---------------------------------------------------------------------------

describe("hasRunningType()", () => {
  it("returns true when a task of that type is currently running", () => {
    useProgressStore.getState().setTasks([
      makeTask("t1", "running", { task_type: "scan" }),
    ]);
    expect(useProgressStore.getState().hasRunningType("scan")).toBe(true);
  });

  it("returns false when no task of that type is running", () => {
    useProgressStore.getState().setTasks([
      makeTask("t1", "running", { task_type: "scan" }),
    ]);
    expect(useProgressStore.getState().hasRunningType("mb_enrichment")).toBe(false);
  });

  it("returns false for a task type that is only present as a terminal row", () => {
    useProgressStore.getState().setTasks([
      makeTask("t1", "running", { task_type: "scan" }),
      makeTask("t2", "failed", { task_type: "mb_enrichment" }),
    ]);
    expect(useProgressStore.getState().hasRunningType("mb_enrichment")).toBe(false);
  });
});
