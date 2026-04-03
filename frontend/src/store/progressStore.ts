import { create } from "zustand";
import type { TaskInfo } from "@/lib/schemas/tasks";

export type ProgressStatus = "IDLE" | "RUNNING" | "COMPLETED" | "FAILED";

interface ProgressState {
  status: ProgressStatus;
  activeTask: TaskInfo | null;
  extraCount: number;
  dismissTimer: ReturnType<typeof setTimeout> | null;
  setTasks: (tasks: TaskInfo[]) => void;
  dismiss: () => void;
}

function pickActiveTask(tasks: TaskInfo[]): TaskInfo | null {
  if (tasks.length === 0) return null;
  // Most recently started task
  return tasks.slice().sort((a, b) =>
    b.started_at.localeCompare(a.started_at)
  )[0] ?? null;
}

export const useProgressStore = create<ProgressState>((set, get) => ({
  status: "IDLE",
  activeTask: null,
  extraCount: 0,
  dismissTimer: null,

  setTasks: (tasks: TaskInfo[]) => {
    const runningTasks = tasks.filter((t) => t.status === "running");
    const failedTasks = tasks.filter((t) => t.status === "failed");
    const completedTasks = tasks.filter((t) => t.status === "completed");

    const { dismissTimer } = get();

    if (runningTasks.length > 0) {
      // Clear any pending dismiss timer when new tasks arrive
      if (dismissTimer) {
        clearTimeout(dismissTimer);
      }
      const active = pickActiveTask(runningTasks);
      set({
        status: "RUNNING",
        activeTask: active,
        extraCount: Math.max(0, runningTasks.length - 1),
        dismissTimer: null,
      });
      return;
    }

    if (failedTasks.length > 0) {
      if (dismissTimer) clearTimeout(dismissTimer);
      const active = pickActiveTask(failedTasks);
      set({
        status: "FAILED",
        activeTask: active,
        extraCount: Math.max(0, failedTasks.length - 1),
        dismissTimer: null,
      });
      return;
    }

    if (completedTasks.length > 0) {
      const { status } = get();
      // Only transition to COMPLETED if we were previously RUNNING
      if (status === "RUNNING") {
        if (dismissTimer) clearTimeout(dismissTimer);
        const active = pickActiveTask(completedTasks);
        const timer = setTimeout(() => {
          set({ status: "IDLE", activeTask: null, extraCount: 0, dismissTimer: null });
        }, 2000);
        set({
          status: "COMPLETED",
          activeTask: active,
          extraCount: Math.max(0, completedTasks.length - 1),
          dismissTimer: timer,
        });
      }
      return;
    }

    // No tasks: go idle if currently running/completed (not if FAILED — user must dismiss)
    const { status } = get();
    if (status === "RUNNING" || status === "COMPLETED") {
      if (dismissTimer) clearTimeout(dismissTimer);
      set({ status: "IDLE", activeTask: null, extraCount: 0, dismissTimer: null });
    }
  },

  dismiss: () => {
    const { dismissTimer } = get();
    if (dismissTimer) clearTimeout(dismissTimer);
    set({ status: "IDLE", activeTask: null, extraCount: 0, dismissTimer: null });
  },
}));
