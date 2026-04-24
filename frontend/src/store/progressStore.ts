import { create } from "zustand";
import type { TaskInfo } from "@/lib/schemas/tasks";

export type ProgressStatus = "IDLE" | "RUNNING" | "COMPLETED" | "FAILED";

interface ProgressState {
  status: ProgressStatus;
  activeTask: TaskInfo | null;
  extraCount: number;
  // All tasks shown in the RUNNING progress bar (running rows plus any
  // terminal rows still inside the WebSocket grace window). Only meaningful
  // when status === "RUNNING". Use `runningTasks` for "is this type in-flight"
  // predicates — it never contains terminal rows.
  visibleTasks: TaskInfo[];
  runningTasks: TaskInfo[];
  // IDs of terminal (completed/failed) tasks currently being displayed.
  // dismiss() reads this to know which IDs to suppress on the next WS tick,
  // keeping it decoupled from the rendering-only visibleTasks field.
  terminalTaskIds: string[];
  dismissTimer: ReturnType<typeof setTimeout> | null;
  // IDs the user has explicitly dismissed. setTasks() filters these out of
  // terminal processing until they expire out of the WS grace window, so a
  // dismiss() call is not immediately reversed by the next WebSocket tick.
  dismissedTaskIds: string[];
  setTasks: (tasks: TaskInfo[]) => void;
  hasRunningType: (type: string) => boolean;
  dismiss: () => void;
}

function pickActiveTask(tasks: TaskInfo[]): TaskInfo | null {
  if (tasks.length === 0) return null;
  // Most recently started task
  return tasks.slice().sort((a, b) => b.started_at.localeCompare(a.started_at))[0] ?? null;
}

export const useProgressStore = create<ProgressState>((set, get) => ({
  status: "IDLE",
  activeTask: null,
  extraCount: 0,
  visibleTasks: [],
  runningTasks: [],
  terminalTaskIds: [],
  dismissTimer: null,
  dismissedTaskIds: [],

  setTasks: (tasks: TaskInfo[]) => {
    const { dismissTimer, dismissedTaskIds } = get();

    // Keep only dismissed IDs still present in the WS payload; the rest have
    // aged out of the grace window and no longer need suppressing.
    const payloadIds = new Set(tasks.map((t) => t.task_id));
    const unexpiredDismissedIds = dismissedTaskIds.filter((id) => payloadIds.has(id));
    const dismissedSet = new Set(unexpiredDismissedIds);

    const runningTasks = tasks.filter((t) => t.status === "running");
    // Exclude user-dismissed tasks from terminal processing so a dismiss()
    // call is not immediately reversed by the next WebSocket tick.
    const failedTasks = tasks.filter((t) => t.status === "failed" && !dismissedSet.has(t.task_id));
    const completedTasks = tasks.filter(
      (t) => t.status === "completed" && !dismissedSet.has(t.task_id)
    );

    if (runningTasks.length > 0) {
      if (dismissTimer) clearTimeout(dismissTimer);
      const active = pickActiveTask(runningTasks);
      // Terminal rows still inside the grace window appear alongside running
      // tasks in the progress bar; track their IDs for dismiss() suppression.
      const graceWindowTerminalIds = tasks
        .filter((t) => t.status === "failed" || t.status === "completed")
        .filter((t) => !dismissedSet.has(t.task_id))
        .map((t) => t.task_id);
      set({
        status: "RUNNING",
        activeTask: active,
        extraCount: Math.max(0, tasks.length - 1),
        visibleTasks: tasks,
        runningTasks,
        terminalTaskIds: graceWindowTerminalIds,
        dismissTimer: null,
        dismissedTaskIds: unexpiredDismissedIds,
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
        visibleTasks: [],
        runningTasks: [],
        terminalTaskIds: failedTasks.map((t) => t.task_id),
        dismissTimer: null,
        dismissedTaskIds: unexpiredDismissedIds,
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
            terminalTaskIds: [],
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
          terminalTaskIds: completedTasks.map((t) => t.task_id),
          dismissTimer: timer,
          dismissedTaskIds: unexpiredDismissedIds,
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
        terminalTaskIds: [],
        dismissTimer: null,
        dismissedTaskIds: [],
      });
      return;
    }

    // No state transition fired, but dismissed IDs that are no longer in the
    // WS payload must still be pruned so they don't linger in IDLE state.
    if (unexpiredDismissedIds.length !== dismissedTaskIds.length) {
      set({ dismissedTaskIds: unexpiredDismissedIds });
    }
  },

  hasRunningType: (type: string) => get().runningTasks.some((t) => t.task_type === type),

  dismiss: () => {
    const { dismissTimer, terminalTaskIds, dismissedTaskIds } = get();
    if (dismissTimer) clearTimeout(dismissTimer);
    // Union existing dismissed IDs with the current terminal IDs. Replacing
    // would "un-dismiss" an earlier failure that is still inside the grace
    // window if a second failure arrives and the user dismisses again.
    const allDismissed = [...new Set([...dismissedTaskIds, ...terminalTaskIds])];
    set({
      status: "IDLE",
      activeTask: null,
      extraCount: 0,
      visibleTasks: [],
      runningTasks: [],
      terminalTaskIds: [],
      dismissTimer: null,
      dismissedTaskIds: allDismissed,
    });
  },
}));
