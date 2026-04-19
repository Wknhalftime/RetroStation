import { CheckCircle, XCircle, X } from "lucide-react";
import { Spinner } from "@/components/ui/Spinner";
import { useProgressStore } from "@/store/progressStore";
import type { TaskInfo } from "@/lib/schemas/tasks";
import { cn } from "@/lib/utils";

const TASK_TYPE_LABELS: Record<string, string> = {
  scan: "Scanning library",
  library_enrichment: "Enriching library metadata",
  mb_enrichment: "Enhancing MusicBrainz entities",
  ingestion: "Ingesting tracks",
  matching: "Matching tracks",
  m3u_export: "Exporting M3U",
  rules_apply: "Applying rules",
};

function getLabel(taskType: string): string {
  return TASK_TYPE_LABELS[taskType] ?? taskType;
}

function getPercent(progressData: Record<string, unknown>): number | null {
  const total = progressData["total"];
  const processed = progressData["processed"];
  if (
    typeof total === "number" &&
    typeof processed === "number" &&
    total > 0
  ) {
    return Math.min(100, Math.round((processed / total) * 100));
  }
  return null;
}

function TaskProgressRow({ task }: { task: TaskInfo }) {
  const label = getLabel(task.task_type);
  const percent = getPercent(task.progress_data);

  return (
    <div className="flex items-center gap-3 px-6 py-2">
      <span className="flex-shrink-0">
        <Spinner className="h-5 w-5 text-blue-500" />
      </span>
      <span className="text-sm font-medium text-gray-700">{label}</span>
      <div className="flex-1 max-w-xs">
        <div className="h-1.5 w-full rounded-full bg-gray-200 overflow-hidden">
          {percent !== null ? (
            <div
              className="h-full rounded-full bg-blue-500 transition-all duration-300"
              style={{ width: `${percent}%` }}
            />
          ) : (
            <div className="h-full w-full rounded-full bg-blue-400 animate-pulse" />
          )}
        </div>
      </div>
      {percent !== null && (
        <span className="text-xs text-gray-500 flex-shrink-0">{percent}%</span>
      )}
    </div>
  );
}

export function ProgressBar() {
  const { status, activeTask, extraCount, runningTasks, dismiss } =
    useProgressStore();

  if (status === "IDLE") return null;

  const containerClasses = cn(
    "fixed bottom-0 left-64 right-0 z-50 flex flex-col divide-y",
    "max-h-48 overflow-y-auto border-t shadow-lg",
    status === "COMPLETED" && "bg-green-50 border-green-200",
    status === "FAILED" && "bg-red-50 border-red-200",
    status === "RUNNING" && "bg-white border-gray-200",
  );

  if (status === "RUNNING") {
    if (runningTasks.length === 0) return null;
    return (
      <div className={containerClasses}>
        {runningTasks.map((task) => (
          <TaskProgressRow key={task.task_id} task={task} />
        ))}
      </div>
    );
  }

  if (activeTask === null) return null;

  const label = getLabel(activeTask.task_type);

  return (
    <div className={containerClasses}>
      <div className="flex items-center gap-3 px-6 py-3">
        <span className="flex-shrink-0">
          {status === "COMPLETED" && (
            <CheckCircle className="h-5 w-5 text-green-500" />
          )}
          {status === "FAILED" && (
            <XCircle className="h-5 w-5 text-red-500" />
          )}
        </span>

        <span
          className={cn(
            "text-sm font-medium",
            status === "COMPLETED" && "text-green-700",
            status === "FAILED" && "text-red-700",
          )}
        >
          {label}
          {status === "COMPLETED" && " — Done"}
          {status === "FAILED" && " — Failed"}
        </span>

        {status === "FAILED" &&
          activeTask?.progress_data?.error != null && (
            <span
              className="text-xs text-red-600 truncate max-w-md"
              title={String(activeTask.progress_data.error)}
            >
              — {String(activeTask.progress_data.error)}
            </span>
          )}

        {status === "COMPLETED" &&
          activeTask?.progress_data?.warning != null && (
            <span className="text-xs text-amber-600 ml-1">
              — No audio files found
            </span>
          )}

        {extraCount > 0 && (
          <span className="text-xs text-gray-500 flex-shrink-0">
            +{extraCount} more
          </span>
        )}

        {status === "FAILED" && (
          <button
            type="button"
            onClick={dismiss}
            className="ml-auto flex-shrink-0 rounded p-1 hover:bg-red-100 text-red-500 transition-colors"
            aria-label="Dismiss"
          >
            <X className="h-4 w-4" />
          </button>
        )}
      </div>
    </div>
  );
}
