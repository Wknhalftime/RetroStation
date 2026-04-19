import { create } from "zustand";
import type { TaskInfo } from "@/lib/schemas/tasks";

export type ProgressStatus = "IDLE" | "RUNNING" | "COMPLETED" | "FAILED";

interface ProgressState {
  status: ProgressStatus;
  activeTask: TaskInfo | null;
  extraCount: number;
  // Tasks rendered by the progress bar during RUNNING, including recently
  // terminal rows still inside the websocket grace window. Use `runningTasks`
  // for predicates that truly mean "in-flight".
  visibleTasks: TaskInfo[];
  runningTasks: TaskInfo[];
  dismissTimer: ReturnType<typeof setTimeout> | null;
  // Task IDs explicitly dismissed by the user. Filtered out of failed/completed
  // processing in setTasks so a dismiss isn't immediately undone by the next
  // WebSocket tick still carrying those rows inside the grace window.
  dismissedTaskIds: string[];
  setTasks: (tasks: TaskInfo[]) => void;
  hasRunningType: (type: string) => boolean;
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
  visibleTasks: [],
  runningTasks: [],
  dismissTimer: null,
  dismissedTaskIds: [],

  setTasks: (tasks: TaskInfo[]) => {
    const { dismissTimer, dismissedTaskIds } = get();

    // Prune dismissed IDs that have expired out of the WS payload entirely
    // so we don't accumulate IDs forever.
    const payloadIds = new Set(tasks.map((t) => t.task_id));
    const activeDismissed = dismissedTaskIds.filter((id) => payloadIds.has(id));
    const dismissedSet = new Set(activeDismissed);

    const runningTasks = tasks.filter((t) => t.status === "running");
    // Exclude user-dismissed tasks from terminal processing so a dismiss()
    // call is not immediately reversed by the next WebSocket tick.
    const failedTasks = tasks.filter(
      (t) => t.status === "failed" && !dismissedSet.has(t.task_id),
    );
    const completedTasks = tasks.filter(
      (t) => t.status === "completed" && !dismissedSet.has(t.task_id),
    );

    if (runningTasks.length > 0) {
      if (dismissTimer) {
        clearTimeout(dismissTimer);
      }
      const active = pickActiveTask(runningTasks);
      set({
        status: "RUNNING",
        activeTask: active,
        extraCount: Math.max(0, tasks.length - 1),
        visibleTasks: tasks,
        runningTasks,
        dismissTimer: null,
        dismissedTaskIds: activeDismissed,
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
        // Populate visibleTasks so dismiss() can collect the IDs to suppress.
        visibleTasks: failedTasks,
        runningTasks: [],
        dismissTimer: null,
        dismissedTaskIds: activeDismissed,
      });
      return;
    }

    if (completedTasks.length > 0) {
      const { status } = get();
      if (status === "RUNNING") {
        if (dismissTimer) clearTimeout(dismissTimer);
        const active = pickActiveTask(completedTasks);
        const timer = setTimeout(() => {
          set({
            status: "IDLE",
            activeTask: null,
            extraCount: 0,
            visibleTasks: [],
            runningTasks: [],
            dismissTimer: null,
            dismissedTaskIds: [],
          });
        }, 2000);
        set({
          status: "COMPLETED",
          activeTask: active,
          extraCount: Math.max(0, completedTasks.length - 1),
          visibleTasks: [],
          runningTasks: [],
          dismissTimer: timer,
          dismissedTaskIds: activeDismissed,
        });
      }
      return;
    }

    const { status } = get();
    if (status === "RUNNING" || status === "COMPLETED") {
      if (dismissTimer) clearTimeout(dismissTimer);
      set({
        status: "IDLE",
        activeTask: null,
        extraCount: 0,
        visibleTasks: [],
        runningTasks: [],
        dismissTimer: null,
        dismissedTaskIds: [],
      });
    }
  },

  hasRunningType: (type: string) =>
    get().runningTasks.some((t) => t.task_type === type),

  dismiss: () => {
    const { dismissTimer, visibleTasks } = get();
    if (dismissTimer) clearTimeout(dismissTimer);
    // Record all currently-visible terminal task IDs so the next setTasks()
    // tick (still carrying those rows inside the WS grace window) doesn't
    // immediately restore the FAILED/COMPLETED state.
    const newDismissed = visibleTasks.map((t) => t.task_id);
    set({
      status: "IDLE",
      activeTask: null,
      extraCount: 0,
      visibleTasks: [],
      runningTasks: [],
      dismissTimer: null,
      dismissedTaskIds: newDismissed,
    });
  },
}));
