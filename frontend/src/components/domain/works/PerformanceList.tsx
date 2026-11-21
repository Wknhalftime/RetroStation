import type { Performance } from "@/lib/schemas/works";
import { CopyRow } from "./CopyRow";
import { NO_CREDITED_ARTIST } from "./credits";
import { PinnedLines } from "./PinnedLines";

// The work's performances (3b PR 5a; S8, D41 a, c, d; C71): each a block named by its credit,
// the master's marked default, with its copies. A performance some line stores says how many
// lines play it, and offers "Prefer this copy" on each copy but its preferred one.

// 3b PR 5b (D47): a performance with no credit (a placeholder work artist, AUD-R078).
export function creditName(performance: Performance): string {
  return performance.credit_text || NO_CREDITED_ARTIST;
}

export function performanceLabel(performance: Performance): string {
  return performance.version_type === "original"
    ? creditName(performance)
    : `${creditName(performance)} (${performance.version_type})`;
}

function linesText(n: number): string {
  return n === 1 ? "1 line plays this performance" : `${n} lines play this performance`;
}

interface PerformanceListProps {
  performances: Performance[];
  masterFileId: string | null;
  isManual: boolean;
  onSetMaster: (fileId: string) => void;
  onPrefer: (performanceId: string, fileId: string) => void;
  onRelease: (identityId: string) => void;
  busy: boolean;
}

export function PerformanceList({
  performances,
  masterFileId,
  isManual,
  onSetMaster,
  onPrefer,
  onRelease,
  busy,
}: PerformanceListProps) {
  return (
    <div className="space-y-4">
      {performances.map((performance) => {
        const stored = !performance.default && performance.lines > 0;
        return (
          <section
            key={performance.id}
            aria-label={performanceLabel(performance)}
            className="overflow-hidden rounded-xl border border-gray-200 bg-white shadow-sm"
          >
            <div className="flex items-center gap-2 bg-gray-50 px-4 py-2">
              <span className="text-sm font-semibold text-gray-800">{creditName(performance)}</span>
              {performance.version_type !== "original" && (
                <span className="rounded bg-gray-200 px-1.5 py-0.5 text-xs text-gray-600">
                  {performance.version_type}
                </span>
              )}
              {performance.default && (
                <span className="rounded bg-amber-100 px-1.5 py-0.5 text-xs font-medium text-amber-800">
                  Default
                </span>
              )}
              {stored && (
                <span className="ml-auto text-xs text-gray-500">
                  {linesText(performance.lines)}
                </span>
              )}
            </div>
            <ul>
              {performance.copies.map(({ file, pins }) => {
                const preferred = file.id === performance.preferred_copy_id;
                const extras = !stored ? undefined : preferred ? (
                  <span className="shrink-0 text-xs font-medium text-indigo-600">
                    Preferred copy
                  </span>
                ) : (
                  <button
                    type="button"
                    disabled={busy || file.file_status !== "present"}
                    onClick={() => onPrefer(performance.id, file.id)}
                    className="shrink-0 rounded border border-gray-300 px-1.5 py-0.5 text-xs text-gray-600 hover:bg-gray-50 disabled:opacity-60"
                  >
                    Prefer this copy
                  </button>
                );
                return (
                  <CopyRow
                    key={file.id}
                    file={file}
                    isMaster={file.id === masterFileId}
                    isManual={isManual}
                    onSetMaster={onSetMaster}
                    extras={extras}
                  >
                    <PinnedLines pins={pins} onRelease={onRelease} releasing={busy} />
                  </CopyRow>
                );
              })}
            </ul>
          </section>
        );
      })}
    </div>
  );
}
