import { Crown } from "lucide-react";
import { cn, formatDuration } from "@/lib/utils";
import type { RecordingDetail } from "@/lib/schemas/works";

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

interface WorkFilesTableProps {
  recordings: RecordingDetail[];
  masterFileId: string | null;
  masterMethod: string | null;
  onSetMaster: (fileId: string) => void;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function basename(filePath: string): string {
  return filePath.split(/[/\\]/).pop() ?? filePath;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function WorkFilesTable({
  recordings,
  masterFileId,
  masterMethod,
  onSetMaster,
}: WorkFilesTableProps) {
  const isManual = masterMethod === "manual";

  return (
    <div className="overflow-hidden rounded-xl border border-gray-200 bg-white shadow-sm">
      {recordings.map((recording, recIdx) => (
        <div key={recording.id}>
          {/* Recording header */}
          <div
            className={cn(
              "flex items-center justify-between bg-gray-50 px-4 py-2",
              recIdx > 0 && "border-t border-gray-200"
            )}
          >
            <div className="flex items-center gap-2">
              <span className="text-sm font-semibold text-gray-800">{recording.title}</span>
              {recording.version_type && (
                <span className="rounded bg-gray-200 px-1.5 py-0.5 text-xs text-gray-600">
                  {recording.version_type}
                </span>
              )}
            </div>
            <span className="text-xs text-gray-400">{formatDuration(recording.duration_ms)}</span>
          </div>

          {/* File rows */}
          {recording.files.map((file, fileIdx) => {
            const isMaster = file.id === masterFileId;
            return (
              <div
                key={file.id}
                className={cn(
                  "flex items-center gap-3 px-4 py-2.5 transition-colors",
                  fileIdx < recording.files.length - 1 && "border-b border-gray-100",
                  isMaster ? "bg-amber-50" : "hover:bg-gray-50"
                )}
              >
                {/* Crown button */}
                <button
                  type="button"
                  onClick={() => onSetMaster(file.id)}
                  aria-label={isMaster ? "Current master file" : "Set as master file"}
                  className={cn(
                    "shrink-0 rounded p-0.5 transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-amber-400",
                    isMaster
                      ? "text-amber-500 hover:text-amber-600"
                      : "text-gray-300 hover:text-amber-400"
                  )}
                >
                  <Crown
                    className="h-4 w-4"
                    fill={isMaster && isManual ? "currentColor" : "none"}
                    strokeWidth={isMaster ? 2 : 1.5}
                  />
                </button>

                {/* File name */}
                <span
                  className={cn(
                    "min-w-0 flex-1 truncate text-sm",
                    isMaster ? "font-medium text-amber-900" : "text-gray-700"
                  )}
                  title={file.file_path}
                >
                  {basename(file.file_path)}
                </span>

                {/* Metadata chips */}
                <div className="flex shrink-0 items-center gap-2 text-xs text-gray-400">
                  <span className="rounded bg-gray-100 px-1.5 py-0.5 font-mono uppercase">
                    {file.format}
                  </span>
                  {file.bitrate != null && <span>{file.bitrate} kbps</span>}
                  <span>{formatDuration(file.duration_ms)}</span>
                </div>
              </div>
            );
          })}
        </div>
      ))}

      {recordings.length === 0 && (
        <div className="px-4 py-8 text-center text-sm text-gray-400">
          No recordings found for this work.
        </div>
      )}
    </div>
  );
}
