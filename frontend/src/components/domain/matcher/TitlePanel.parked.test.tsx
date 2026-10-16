// @vitest-environment jsdom
// Spec 2026-10-13 R5: songs out of review sit in one collapsed "No likely match" section;
// Resolved holds matched songs only.
import { describe, it, expect, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import type { ProposedMatch, QueueArtist, QueueIdentity } from "@/lib/schemas/matcher";

vi.mock("@/api/client", () => ({ apiFetch: vi.fn() }));

const mutateMock = vi.fn();
const unmatchMutateMock = vi.fn();
vi.mock("@/api/matcher", () => ({
  useResolveIdentity: () => ({ mutate: mutateMock, isPending: false }),
  useUnmatchIdentity: () => ({ mutate: unmatchMutateMock, isPending: false }),
}));

import { TitlePanel } from "./TitlePanel";

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

function song(idSuffix: string, overrides: Partial<QueueIdentity>): QueueIdentity {
  return {
    id: `00000000-0000-0000-0000-0000000000${idSuffix}`,
    original_title: `Song ${idSuffix}`,
    normalized_title: `song ${idSuffix}`,
    match_status: "needs_review",
    match_tier: null,
    triage_bucket: "quick_review",
    ...overrides,
  };
}

function parked(idSuffix: string, title: string): QueueIdentity {
  return song(idSuffix, {
    original_title: title,
    match_status: "manual_rejected",
    triage_bucket: "blocked",
    confidence_score: null,
    reason_code: "USER_UNMATCHED",
    reason_detail: null,
  });
}

function artist(identities: QueueIdentity[]): QueueArtist {
  return {
    id: "00000000-0000-0000-0000-000000000001",
    original_name: "R.E.M.",
    normalized_name: "rem",
    match_status: "auto_matched",
    triage_bucket: "quick_review",
    candidates: [],
    identities,
  };
}

function proposed(): ProposedMatch {
  return {
    library_file_id: "00000000-0000-0000-0000-0000000000aa",
    file_path: "/music/rem/fall-on-me.flac",
    track_title: "Fall On Me",
    release_title: "Lifes Rich Pageant",
    recording_mbid: null,
    candidate_match_tier: "local_file_fuzzy",
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  window.localStorage.clear();
});

describe("TitlePanel: songs out of review", () => {
  it("puts a song you rejected under No likely match with a plain-language chip", () => {
    render(
      <TitlePanel
        artist={artist([
          song("01", { original_title: "Review me", confidence_score: 70 }),
          parked("02", "Begin The Begin"),
        ])}
        onFileSearch={vi.fn()}
      />,
      { wrapper }
    );
    expect(screen.getByRole("heading", { name: /Needs Review \(1\)/i })).toBeDefined();
    const toggle = screen.getByRole("button", { name: /No likely match \(1\)/i });
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByText("Begin The Begin")).toBeNull();

    fireEvent.click(toggle);
    expect(screen.getByText("Begin The Begin")).toBeDefined();
    expect(screen.getByText("Rejected by you · waiting for re-match")).toBeDefined();
    expect(screen.queryByText(/USER_UNMATCHED/)).toBeNull();
  });

  it("puts an auto-rejected song under No likely match, not Resolved", () => {
    render(
      <TitlePanel
        artist={artist([
          song("01", { original_title: "Drive", match_status: "auto_matched" }),
          song("02", {
            original_title: "Fall On Me",
            match_status: "auto_rejected",
            triage_bucket: "blocked",
            confidence_score: null,
            reason_code: "LOW_CONFIDENCE",
          }),
        ])}
        onFileSearch={vi.fn()}
      />,
      { wrapper }
    );
    // Needs Review is empty, so No likely match opens by default.
    expect(screen.getByRole("heading", { name: /Needs Review \(0\)/i })).toBeDefined();
    expect(screen.getByRole("heading", { name: /Resolved \(1\)/i })).toBeDefined();
    const toggle = screen.getByRole("button", { name: /No likely match \(1\)/i });
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByText("Fall On Me")).toBeDefined();
    expect(screen.getByText("Likely not in library")).toBeDefined();
  });

  it("counts only matched songs in the Resolved breakdown", () => {
    render(
      <TitlePanel
        artist={artist([
          song("01", { match_status: "auto_matched" }),
          song("02", { match_status: "auto_matched" }),
          song("03", { match_status: "manual_matched" }),
          parked("04", "Driver 8"),
          song("05", { match_status: "auto_rejected", triage_bucket: "blocked" }),
        ])}
        onFileSearch={vi.fn()}
      />,
      { wrapper }
    );
    expect(screen.getByRole("heading", { name: /Resolved \(3\)/i })).toBeDefined();
    const header = screen.getByRole("button", { name: /Resolved/i });
    expect(header.textContent).toContain("2 auto");
    expect(header.textContent).toContain("1 manual");
    expect(header.textContent).not.toContain("rejected");
  });

  it("keeps Find File on a parked row, wired to that song", () => {
    const onFileSearch = vi.fn();
    const target = parked("02", "Pop Song 89");
    render(<TitlePanel artist={artist([target])} onFileSearch={onFileSearch} />, { wrapper });
    // Only out-of-review songs: the section is open by default.
    fireEvent.click(screen.getByRole("button", { name: /Find File/i }));
    expect(onFileSearch).toHaveBeenCalledWith(target.id);
  });

  it("shows no Reject or Approve on an out-of-review song that carries an old suggestion", () => {
    render(
      <TitlePanel
        artist={artist([
          song("01", {
            original_title: "Superman",
            match_status: "auto_rejected",
            triage_bucket: "needs_attention",
            confidence_score: 60,
            proposed_match: proposed(),
          }),
        ])}
        onFileSearch={vi.fn()}
      />,
      { wrapper }
    );
    const section = screen.getByRole("button", { name: /No likely match \(1\)/i }).closest("section");
    expect(section).not.toBeNull();
    const scope = within(section as HTMLElement);
    expect(scope.getByText("Superman")).toBeDefined();
    expect(scope.queryByRole("button", { name: /^Reject$/i })).toBeNull();
    expect(scope.queryByRole("button", { name: /^Approve$/i })).toBeNull();
    expect(scope.getByRole("button", { name: /Find File/i })).toBeDefined();
  });
});
