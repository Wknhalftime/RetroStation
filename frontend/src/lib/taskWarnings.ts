/**
 * Human-readable text for a task's `progress_data.warning` code.
 *
 * A warning marks a task that completed but did not do everything the
 * operator expected — most importantly a CSV import that committed some
 * rows and dropped others. The backend emits stable codes; the wording
 * lives here so the UI can change without a migration.
 */

const WARNING_LABELS: Record<string, string> = {
  no_audio_files_found: "No audio files found",
};

/** Skip-reason codes emitted by the ingestion service. */
const SKIP_REASON_LABELS: Record<string, string> = {
  blank_required_field: "blank Artist/Title/Played",
  extra_fields: "too many columns",
};

const ROWS_SKIPPED = "rows_skipped";
// A matching run that completed with some playlist stages failed (spec 2026-10-17); the
// count is in `stages_failed`.
const MATCHING_STAGES_FAILED = "matching_stages_failed";
// A cache fill run whose calls failed (phase 4.1 PR 2b, D10); the count is in `failed_calls`.
const FILL_CALLS_FAILED = "mb_fill_calls_failed";

function formatFailedFillCalls(progressData: Record<string, unknown>): string {
  const failed = progressData["failed_calls"];
  const count = typeof failed === "number" ? failed : 0;
  return count === 1
    ? "1 call failed: a later run asks it again"
    : `${count.toLocaleString("en-US")} calls failed: a later run asks them again`;
}

function formatFailedStages(progressData: Record<string, unknown>): string {
  const failed = progressData["stages_failed"];
  const count = typeof failed === "number" ? failed : 0;
  const noun = count === 1 ? "playlist stage" : "playlist stages";
  return `${count.toLocaleString()} ${noun} failed — see System Logs`;
}

function formatSkipReasons(raw: unknown): string {
  if (typeof raw !== "object" || raw === null) return "";
  const parts = Object.entries(raw as Record<string, unknown>)
    .filter(([, count]) => typeof count === "number")
    .map(([code, count]) => `${SKIP_REASON_LABELS[code] ?? code}: ${count}`);
  return parts.length > 0 ? ` (${parts.join(", ")})` : "";
}

export function getWarningText(
  progressData: Record<string, unknown>
): string | null {
  const warning = progressData["warning"];
  if (typeof warning !== "string") return null;
  if (warning === MATCHING_STAGES_FAILED) return formatFailedStages(progressData);
  if (warning === FILL_CALLS_FAILED) return formatFailedFillCalls(progressData);
  if (warning !== ROWS_SKIPPED) return WARNING_LABELS[warning] ?? warning;

  const skipped = progressData["skipped"];
  const count = typeof skipped === "number" ? skipped : 0;
  const noun = count === 1 ? "row" : "rows";
  const detail = formatSkipReasons(progressData["skip_reasons"]);
  return `${count.toLocaleString()} ${noun} skipped${detail}`;
}
