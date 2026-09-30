// @vitest-environment jsdom
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import type { QueueArtist, QueueIdentity } from "@/lib/schemas/matcher";

vi.mock("@/api/client", () => ({ apiFetch: vi.fn() }));
vi.mock("@/api/matcher", () => ({
  useResolveIdentity: () => ({ mutate: vi.fn(), isPending: false }),
  useUnmatchIdentity: () => ({ mutate: vi.fn(), isPending: false }),
}));

import { TitlePanel } from "./TitlePanel";

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

// A needs_review item with no score, as the queue sends one whose match rows were deleted.
function unscored(suffix: string, title: string, reasonCode: string): QueueIdentity {
  return {
    id: `00000000-0000-0000-0000-0000000000${suffix}`,
    original_title: title,
    normalized_title: title.toLowerCase(),
    match_status: "needs_review",
    match_tier: null,
    triage_bucket: "blocked",
    confidence_score: null,
    reason_code: reasonCode,
  };
}

function artistWith(identities: QueueIdentity[]): QueueArtist {
  return {
    id: "00000000-0000-0000-0000-000000000001",
    original_name: "Prince",
    normalized_name: "prince",
    // Resolved status is required or TitlePanel short-circuits.
    match_status: "manual_matched",
    triage_bucket: "blocked",
    candidates: [],
    identities,
  };
}

describe("TitlePanel: an identity released by a deleted missing file", () => {
  it("is likely: listed under Needs Review, not behind No likely match", () => {
    const artist = artistWith([
      unscored("01", "Released Song", "LIBRARY_FILE_REMOVED"),
      unscored("02", "Low Guess", "LOW_CONFIDENCE"),
    ]);
    render(<TitlePanel artist={artist} onFileSearch={vi.fn()} />, { wrapper });

    expect(screen.getByRole("heading", { name: /Needs Review \(1\)/i })).toBeDefined();
    expect(screen.getByText("Released Song")).toBeDefined();
    // The guard: an unscored item with another reason stays behind the collapsed toggle.
    const toggle = screen.getByRole("button", { name: /No likely match \(1\)/i });
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByText("Low Guess")).toBeNull();
  });
});
