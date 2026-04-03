import { CheckCircle, XCircle, X } from "lucide-react";
import { Spinner } from "@/components/ui/Spinner";
import { useProgressStore } from "@/store/progressStore";
import { cn } from "@/lib/utils";

const TASK_TYPE_LABELS: Record<string, string> = {
  scan: "Scanning library",
  enrichment: "Enriching metadata",
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

export function ProgressBar() {
  const { status, activeTask, extraCount, dismiss } = useProgressStore();

  if (status === "IDLE" || activeTask === null) return null;

  const label = getLabel(activeTask.task_type);
  const percent =
    status === "RUNNING" ? getPercent(activeTask.progress_data) : null;

  return (
    <div
      className={cn(
        "fixed bottom-0 left-64 right-0 z-50 flex items-center gap-3 px-6 py-3 shadow-lg border-t",
        status === "COMPLETED" && "bg-green-50 border-green-200",
        status === "FAILED" && "bg-red-50 border-red-200",
        status === "RUNNING" && "bg-white border-gray-200"
      )}
    >
      {/* Status icon */}
      <span className="flex-shrink-0">
        {status === "RUNNING" && (
          <Spinner className="h-5 w-5 text-blue-500" />
        )}
        {status === "COMPLETED" && (
          <CheckCircle className="h-5 w-5 text-green-500" />
        )}
        {status === "FAILED" && (
          <XCircle className="h-5 w-5 text-red-500" />
        )}
      </span>

      {/* Label */}
      <span
        className={cn(
          "text-sm font-medium",
          status === "COMPLETED" && "text-green-700",
          status === "FAILED" && "text-red-700",
          status === "RUNNING" && "text-gray-700"
        )}
      >
        {label}
        {status === "COMPLETED" && " — Done"}
        {status === "FAILED" && " — Failed"}
      </span>

      {/* Progress bar */}
      {status === "RUNNING" && percent !== null && (
        <div className="flex-1 max-w-xs">
          <div className="h-1.5 w-full rounded-full bg-gray-200 overflow-hidden">
            <div
              className="h-full rounded-full bg-blue-500 transition-all duration-300"
              style={{ width: `${percent}%` }}
            />
          </div>
        </div>
      )}

      {/* Percent label */}
      {status === "RUNNING" && percent !== null && (
        <span className="text-xs text-gray-500 flex-shrink-0">{percent}%</span>
      )}

      {/* Extra count */}
      {extraCount > 0 && (
        <span className="text-xs text-gray-500 flex-shrink-0">
          +{extraCount} more
        </span>
      )}

      {/* Dismiss button — only on FAILED */}
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
  );
}
