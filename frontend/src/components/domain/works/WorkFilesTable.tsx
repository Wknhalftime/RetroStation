import { Link } from "react-router-dom";
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

const CUES_LINK = cn(
  "shrink-0 rounded border border-gray-200 px-1.5 py-0.5 text-xs font-medium text-gray-600",
  "hover:border-indigo-300 hover:text-indigo-600"
);

function basename(filePath: string): string {
  return filePath.split(/[/\\]/).pop() ?? filePath;
}

function crownLabel(isMaster: boolean, fromOtherWork: boolean): string {
  if (isMaster) return "Current master file";
  if (fromOtherWork) return "Move into this song and set as master file";
  return "Set as master file";
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
            const isMissing = file.file_status !== "present";
            const otherWork = file.other_work ?? null;
            return (
              <div
                key={file.id}
                className={cn(
                  "flex items-center gap-3 px-4 py-2.5 transition-colors",
                  fileIdx < recording.files.length - 1 && "border-b border-gray-100",
                  isMaster ? "bg-amber-50" : "hover:bg-gray-50",
                  isMissing && "opacity-60"
                )}
              >
                {/* Crown button */}
                {isMissing ? (
                  <span className="h-5 w-5 shrink-0" aria-hidden="true" />
                ) : (
                  <button
                    type="button"
                    onClick={() => onSetMaster(file.id)}
                    aria-label={crownLabel(isMaster, otherWork !== null)}
                    title={
                      otherWork && !isMaster
                        ? "Moves this file into this song, then makes it the master"
                        : undefined
                    }
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
                )}

                {/* File name, and the album it is from */}
                <div className="min-w-0 flex-1">
                  <div
                    className={cn(
                      "truncate text-sm",
                      isMaster ? "font-medium text-amber-900" : "text-gray-700"
                    )}
                    title={file.file_path}
                  >
                    {basename(file.file_path)}
                  </div>
                  {file.release_title && (
                    <div
                      className="truncate text-xs text-gray-400"
                      title={file.release_title}
                      data-testid="file-album"
                    >
                      {file.release_title}
                    </div>
                  )}
                  {otherWork && (
                    <div
                      className="truncate text-xs text-indigo-500"
                      title="This file shares a recording with this song but sits on another"
                      data-testid="file-other-work"
                    >
                      on {otherWork.artist_name} › {otherWork.title}
                    </div>
                  )}
                </div>

                {isMissing && (
                  <span
                    className="shrink-0 rounded bg-red-100 px-1.5 py-0.5 text-xs font-medium text-red-700"
                    title="This file is no longer on disk"
                  >
                    Missing
                  </span>
                )}

                {/* The cue editor, for a file that is on disk */}
                {!isMissing && (
                  <Link
                    to={`/library/files/${file.id}/cues`}
                    className={CUES_LINK}
                    title="Edit where the radio starts, fades and stops this file"
                  >
                    Cues
                  </Link>
                )}

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
