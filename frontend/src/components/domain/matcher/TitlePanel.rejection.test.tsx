// @vitest-environment jsdom
// Acceptance tests: a review card rejects the suggestion it shows; Reject needs a suggestion;
// a matched row's Reject sends no file (the server records the matched file). Spec 2026-10-05
// §4.1, decisions D3 and D5.
import { describe, it, expect, vi, beforeEach } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import type { ProposedMatch, QueueArtist, QueueIdentity } from "@/lib/schemas/matcher";

vi.mock("@/api/client", () => ({ apiFetch: vi.fn() }));

const mutateMock = vi.fn();
const useResolveIdentityMock = vi.fn(() => ({ mutate: mutateMock, isPending: false }));
const unmatchMutateMock = vi.fn();
const useUnmatchIdentityMock = vi.fn(() => ({ mutate: unmatchMutateMock, isPending: false }));

vi.mock("@/api/matcher", () => ({
  useResolveIdentity: () => useResolveIdentityMock(),
  useUnmatchIdentity: () => useUnmatchIdentityMock(),
}));

import { TitlePanel } from "./TitlePanel";

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

function identity(overrides: Partial<QueueIdentity> = {}): QueueIdentity {
  return {
    id: "00000000-0000-0000-0000-000000000010",
    original_title: "Purple Rain",
    normalized_title: "purple rain",
    match_status: "needs_review",
    match_tier: null,
    triage_bucket: "quick_review",
    ...overrides,
  };
}

function proposed(): ProposedMatch {
  return {
    library_file_id: "00000000-0000-0000-0000-0000000000aa",
    file_path: "/music/prince/purple-rain.flac",
    track_title: "Purple Rain",
    release_title: "Purple Rain",
    recording_mbid: "rec-mbid-0001",
    candidate_match_tier: "local_file_fuzzy",
  };
}

function artist(identities: QueueIdentity[]): QueueArtist {
  return {
    id: "00000000-0000-0000-0000-000000000001",
    original_name: "Prince",
    normalized_name: "prince",
    match_status: "manual_matched",
    triage_bucket: "quick_review",
    candidates: [],
    identities,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  useResolveIdentityMock.mockReturnValue({ mutate: mutateMock, isPending: false });
  useUnmatchIdentityMock.mockReturnValue({ mutate: unmatchMutateMock, isPending: false });
});

describe("TitlePanel rejection", () => {
  it("a review card's Reject sends the suggestion it shows", () => {
    render(<TitlePanel artist={artist([identity({ proposed_match: proposed() })])} onFileSearch={vi.fn()} />, {
      wrapper,
    });
    fireEvent.click(screen.getByRole("button", { name: /^Reject$/i }));
    expect(mutateMock).toHaveBeenCalledWith({
      id: "00000000-0000-0000-0000-000000000010",
      resolution: {
        match_status: "manual_rejected",
        library_file_id: "00000000-0000-0000-0000-0000000000aa",
      },
    });
  });

  it("a review card without a suggestion has no Reject", () => {
    render(<TitlePanel artist={artist([identity({ proposed_match: null })])} onFileSearch={vi.fn()} />, {
      wrapper,
    });
    expect(screen.queryByRole("button", { name: /^Reject$/i })).toBeNull();
    expect(screen.getByRole("button", { name: /Find File/i })).toBeDefined();
  });

  it("a matched row's Reject sends no file", () => {
    render(
      <TitlePanel
        artist={artist([identity({ match_status: "auto_matched", proposed_match: null })])}
        onFileSearch={vi.fn()}
      />,
      { wrapper }
    );
    fireEvent.click(screen.getByRole("button", { name: /Resolved/i }));
    fireEvent.click(screen.getByRole("button", { name: /Reject Purple Rain/i }));
    expect(mutateMock).toHaveBeenCalledWith({
      id: "00000000-0000-0000-0000-000000000010",
      resolution: { match_status: "manual_rejected", library_file_id: null },
    });
  });
});
