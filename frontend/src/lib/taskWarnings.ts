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
  if (warning !== ROWS_SKIPPED) return WARNING_LABELS[warning] ?? warning;

  const skipped = progressData["skipped"];
  const count = typeof skipped === "number" ? skipped : 0;
  const noun = count === 1 ? "row" : "rows";
  const detail = formatSkipReasons(progressData["skip_reasons"]);
  return `${count.toLocaleString()} ${noun} skipped${detail}`;
}
