export function formatDuration(ms: number | null | undefined): string {
  if (ms == null || ms <= 0) return "—";
  const totalSeconds = Math.floor(ms / 1000);
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${minutes}:${seconds.toString().padStart(2, "0")}`;
}

export function formatDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleDateString("en-US", {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

const DATE_TIME_OPTIONS: Intl.DateTimeFormatOptions = {
  year: "numeric",
  month: "short",
  day: "numeric",
  hour: "2-digit",
  minute: "2-digit",
};

/** Format a real instant (created_at, ingested_at, ...) in the browser's time zone. */
export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString("en-US", DATE_TIME_OPTIONS);
}

/**
 * Format a station wall-clock time (played_at) exactly as it was logged.
 *
 * played_at stores the station's local time labelled as UTC, so its UTC reading
 * is the wall clock. Formatting in UTC makes the result independent of both the
 * browser's zone and whatever offset the API serialized the value with.
 */
export function formatWallClock(iso: string | null | undefined): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString("en-US", { ...DATE_TIME_OPTIONS, timeZone: "UTC" });
}

export function formatConfidence(score: number | null | undefined): string {
  if (score == null) return "—";
  return `${Math.round(score * 100)}%`;
}

export function cn(...classes: (string | false | null | undefined)[]): string {
  return classes.filter(Boolean).join(" ");
}
