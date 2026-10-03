import { useEffect, useRef } from "react";
import { useCueCoverage } from "@/api/streaming";
import { useProgressStore } from "@/store/progressStore";
import { CUE_RUN_LABEL, getPercent } from "@/components/layout/ProgressBar";
import { Spinner } from "@/components/ui/Spinner";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function describeLoadError(error: unknown): string {
  return error instanceof Error ? error.message : "The cue coverage could not be loaded.";
}

function wholeNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isInteger(value) && value >= 0 ? value : null;
}

interface ReadyOf {
  settled: number;
  analysable: number;
}

/** M4: while a run is live, "X of Y" follows its row (processed of total), which moves
 * after every batch; the page's own count is read once. Both only grow during a run, so
 * the larger of each is the newer, and the text never goes back. The run's end re-reads
 * the real counts (VG3). */
function liveReadyOf(counted: ReadyOf, progressData: Record<string, unknown>): ReadyOf {
  const processed = wholeNumber(progressData["processed"]);
  const total = wholeNumber(progressData["total"]);
  if (processed === null || total === null) return counted;
  return {
    settled: Math.max(counted.settled, processed),
    analysable: Math.max(counted.analysable, total),
  };
}

// ---------------------------------------------------------------------------
// Component — D77/PG7: "cues ready X of Y" (D89a: X is the settled audio,
// ready plus failed; the failed count is also shown on its own). D89/D77a:
// while a cue run is live, its progress shows here too, read from the same
// progress row the bottom bar reads (no new mechanism), and "X of Y"
// follows that row (M4) — and when the run completes, the count is re-read
// (VG3).
// ---------------------------------------------------------------------------

export function CueCoverage() {
  const { data, isLoading, isError, error, refetch } = useCueCoverage();
  const runningRun = useProgressStore(
    (s) => s.runningTasks.find((t) => t.task_type === "cue_analysis") ?? null
  );
  const wasRunningRef = useRef(false);

  useEffect(() => {
    const isRunning = runningRun !== null;
    // A cue run was live and now is not (it finished, failed off the bar, or
    // simply dropped out of the feed) — re-read the counts (VG3). The route
    // opens its own connection per call, so this is the only trigger; there
    // is no interval poll.
    if (wasRunningRef.current && !isRunning) {
      void refetch();
    }
    wasRunningRef.current = isRunning;
  }, [runningRun, refetch]);

  if (isError) {
    return (
      <div>
        <h3 className="mb-2 text-sm font-semibold text-gray-700">Cue analysis</h3>
        <p role="alert" className="rounded-md bg-red-50 p-3 text-sm text-red-700">
          {describeLoadError(error)}
        </p>
      </div>
    );
  }

  if (isLoading || !data) {
    return (
      <div>
        <h3 className="mb-2 text-sm font-semibold text-gray-700">Cue analysis</h3>
        <div role="status" aria-label="Loading cue coverage" className="flex justify-center py-6">
          <Spinner className="h-5 w-5 text-indigo-500" />
        </div>
      </div>
    );
  }

  const counted = { settled: data.ready + data.failed, analysable: data.analysable };
  const { settled, analysable } = runningRun
    ? liveReadyOf(counted, runningRun.progress_data)
    : counted;
  const percent = runningRun ? getPercent(runningRun.progress_data) : null;

  return (
    <div>
      <h3 className="mb-2 text-sm font-semibold text-gray-700">Cue analysis</h3>
      <p className="text-sm text-gray-700">
        {settled.toLocaleString()} of {analysable.toLocaleString()} cues ready
      </p>
      <p className="mt-1 text-xs text-gray-400">
        {data.failed.toLocaleString()} failed to analyse; {data.unhashed.toLocaleString()} have no
        audio fingerprint yet.
      </p>

      {runningRun && (
        <div className="mt-3">
          <p className="mb-1 text-xs text-gray-500">{CUE_RUN_LABEL}…</p>
          <div
            role="progressbar"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={percent ?? undefined}
            className="h-1.5 w-full max-w-xs rounded-full bg-gray-200 overflow-hidden"
          >
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
      )}
    </div>
  );
}
