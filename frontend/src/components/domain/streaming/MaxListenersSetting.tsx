import { useState, useEffect } from "react";
import { useStreamSettings, useSetMaxSessions } from "@/api/streaming";
import { useProgressStore } from "@/store/progressStore";
import type { TaskInfo } from "@/lib/schemas/tasks";
import { Spinner } from "@/components/ui/Spinner";

// ---------------------------------------------------------------------------
// Validation — D27: a whole number of 1 or more, checked here before the
// server checks again.
// ---------------------------------------------------------------------------

function parseLimit(raw: string): number | null {
  const trimmed = raw.trim();
  if (trimmed === "") return null;
  const value = Number(trimmed);
  if (!Number.isFinite(value) || !Number.isInteger(value) || value < 1) return null;
  return value;
}

function describeError(error: unknown): string {
  return error instanceof Error ? error.message : "The limit could not be saved.";
}

// ---------------------------------------------------------------------------
// D91: half the threads, a quarter of the memory — the live meter row's
// suggestion. With streaming off there is no meter row, so no suggestion.
// A limit above it is allowed, with a warning (disk is not counted, so the
// suggestion is deliberately conservative).
// ---------------------------------------------------------------------------

function readSuggestedMax(pageTasks: TaskInfo[]): number | null {
  const meter = pageTasks.find((t) => t.task_type === "stream_resources");
  if (!meter || meter.status !== "running") return null;
  const value = meter.progress_data["suggested_max"];
  return typeof value === "number" && value >= 1 ? value : null;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function MaxListenersSetting() {
  const { data, isLoading, isError, error } = useStreamSettings();
  const setMaxSessions = useSetMaxSessions();
  const pageTasks = useProgressStore((s) => s.pageTasks);
  const suggested = readSuggestedMax(pageTasks);
  const [value, setValue] = useState("");
  const [refusal, setRefusal] = useState<string | null>(null);

  // Only a change to the stored limit replaces the field: a refetch for another reason (an
  // upload or a removal of the sign-off clip) keeps a limit the user is still typing.
  const storedLimit = data?.max_sessions;
  useEffect(() => {
    if (storedLimit !== undefined) setValue(storedLimit === null ? "" : String(storedLimit));
  }, [storedLimit]);

  if (isError) {
    return (
      <div>
        <h3 className="mb-2 text-sm font-semibold text-gray-700">Max listeners</h3>
        <p role="alert" className="rounded-md bg-red-50 p-3 text-sm text-red-700">
          {describeError(error)}
        </p>
      </div>
    );
  }

  // Before the stored limit has loaded, the field has nothing real to show. An empty input
  // looks like "no limit" rather than "still loading", and Save would act on it, so show a
  // neutral loading state and render neither the input nor Save until the data arrives.
  if (isLoading || !data) {
    return (
      <div>
        <h3 className="mb-2 text-sm font-semibold text-gray-700">Max listeners</h3>
        <div
          role="status"
          aria-label="Loading max listeners setting"
          className="flex justify-center py-6"
        >
          <Spinner className="h-5 w-5 text-indigo-500" />
        </div>
      </div>
    );
  }

  function handleSave() {
    const parsed = parseLimit(value);
    if (parsed === null) {
      setRefusal("Enter a whole number of 1 or more.");
      return;
    }
    setRefusal(null);
    setMaxSessions.mutate(parsed, {
      onError: (mutationError) => setRefusal(describeError(mutationError)),
    });
  }

  const problem = refusal ?? data.max_sessions_problem ?? null;
  const parsedValue = parseLimit(value);
  const aboveSuggestion = suggested !== null && parsedValue !== null && parsedValue > suggested;

  function fillSuggested() {
    if (suggested === null) return;
    setValue(String(suggested));
    setRefusal(null);
  }

  return (
    <div>
      <h3 className="mb-2 text-sm font-semibold text-gray-700">Max listeners</h3>
      <div className="flex items-center gap-3">
        <label className="flex items-center gap-2 text-sm text-gray-700">
          Max listeners
          <input
            type="number"
            aria-label="Max listeners"
            value={value}
            onChange={(e) => {
              setValue(e.target.value);
              setRefusal(null);
            }}
            className="w-24 rounded-md border border-gray-300 px-2 py-1 text-sm"
          />
        </label>
        <button
          type="button"
          onClick={handleSave}
          disabled={setMaxSessions.isPending}
          className="inline-flex items-center rounded-md bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-indigo-700 disabled:opacity-60"
        >
          Save
        </button>
        {suggested !== null && (
          <button
            type="button"
            onClick={fillSuggested}
            className="inline-flex items-center rounded-md border border-gray-300 px-3 py-1.5 text-sm font-medium text-gray-700 hover:bg-gray-100"
          >
            Use suggested ({suggested})
          </button>
        )}
      </div>
      <p className="mt-1 text-xs text-gray-400">
        The most listeners allowed at once. Lowering it does not stop anyone already listening.
      </p>
      {aboveSuggestion && (
        <p role="alert" className="mt-2 rounded-md bg-amber-50 p-3 text-sm text-amber-700">
          Above the suggested maximum of {suggested} listeners for this machine right now (half the
          processor threads, a quarter of the memory — disk is not counted). It is allowed; this
          machine may run short under load.
        </p>
      )}
      {problem && (
        <p role="alert" className="mt-2 rounded-md bg-red-50 p-3 text-sm text-red-700">
          {problem}
        </p>
      )}
    </div>
  );
}
