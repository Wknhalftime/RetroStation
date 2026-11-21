import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { Crown } from "lucide-react";
import { cn, formatDuration } from "@/lib/utils";
import type { FileInfo } from "@/lib/schemas/works";
import { NO_CREDITED_ARTIST } from "./credits";

// One copy of a song on the work page (3b PR 5a, S8): what the retired "Recordings & Files"
// table showed of a file, with the crown that makes it the master; *extras* sit beside it
// (Prefer this copy) and *children* under it (its pinned lines).

const CUES_LINK = cn(
  "shrink-0 rounded border border-gray-200 px-1.5 py-0.5 text-xs font-medium text-gray-600",
  "hover:border-indigo-300 hover:text-indigo-600"
);

export function basename(filePath: string): string {
  return filePath.split(/[/\\]/).pop() ?? filePath;
}

function crownLabel(isMaster: boolean, fromOtherWork: boolean): string {
  if (isMaster) return "Current master file";
  if (fromOtherWork) return "Move into this song and set as master file";
  return "Set as master file";
}

interface CopyRowProps {
  file: FileInfo;
  isMaster: boolean;
  isManual: boolean;
  onSetMaster: (fileId: string) => void;
  extras?: ReactNode;
  children?: ReactNode;
}

export function CopyRow({ file, isMaster, isManual, onSetMaster, extras, children }: CopyRowProps) {
  const isMissing = file.file_status !== "present";
  const otherWork = file.other_work ?? null;
  const otherArtists = otherWork?.artists?.length
    ? otherWork.artists.join(", ")
    : NO_CREDITED_ARTIST;
  return (
    <li aria-label={basename(file.file_path)} className="border-b border-gray-100 last:border-0">
      <div
        className={cn(
          "flex items-center gap-3 px-4 py-2.5",
          isMaster ? "bg-amber-50" : "hover:bg-gray-50",
          isMissing && "opacity-60"
        )}
      >
        {isMissing ? (
          <span className="h-5 w-5 shrink-0" aria-hidden="true" />
        ) : (
          <button
            type="button"
            onClick={() => onSetMaster(file.id)}
            aria-label={crownLabel(isMaster, otherWork !== null)}
            className={cn(
              "shrink-0 rounded p-0.5 focus:outline-none focus-visible:ring-2 focus-visible:ring-amber-400",
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
            <div className="truncate text-xs text-gray-400" data-testid="file-album">
              {file.release_title}
            </div>
          )}
          {otherWork && (
            <div className="truncate text-xs text-indigo-500">
              on{" "}
              <Link to={`/library/works/${otherWork.id}`} className="hover:underline">
                {otherArtists} › {otherWork.title}
              </Link>
            </div>
          )}
        </div>

        {isMissing && (
          <span className="shrink-0 rounded bg-red-100 px-1.5 py-0.5 text-xs font-medium text-red-700">
            Missing
          </span>
        )}
        {extras}
        {!isMissing && (
          <Link to={`/library/files/${file.id}/cues`} className={CUES_LINK}>
            Cues
          </Link>
        )}
        <div className="flex shrink-0 items-center gap-2 text-xs text-gray-400">
          <span className="rounded bg-gray-100 px-1.5 py-0.5 font-mono uppercase">
            {file.format}
          </span>
          {file.bitrate != null && <span>{file.bitrate} kbps</span>}
          <span>{formatDuration(file.duration_ms)}</span>
        </div>
      </div>
      {children}
    </li>
  );
}
