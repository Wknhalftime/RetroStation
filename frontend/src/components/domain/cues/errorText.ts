import { ValidationError } from "@/api/client";

/** The text to show for a failed cue editor request: the server's reason where it gave one. */
export function errorText(error: Error): string {
  if (error instanceof ValidationError) {
    // FastAPI sends a list of {msg}; a hand-raised 422 may send a plain string.
    const detail: unknown = error.detail;
    if (typeof detail === "string" && detail) return detail;
    if (Array.isArray(detail)) {
      const reasons = detail
        .map((item: unknown) =>
          typeof item === "object" && item !== null && "msg" in item ? String(item.msg) : ""
        )
        .filter(Boolean);
      if (reasons.length > 0) return reasons.join(" ");
    }
  }
  return error.message;
}
