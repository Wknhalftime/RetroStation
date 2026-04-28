// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import type { ProposedMatch, QueueArtist, QueueIdentity } from "@/lib/schemas/matcher";

vi.mock("@/api/client", () => ({
  apiFetch: vi.fn(),
}));

// Mock useResolveIdentity directly so we can force isPending true/false
// deterministically (the “all three buttons disabled while pending” test
// otherwise depends on real mutation timing, which is flaky under jsdom).
const mutateMock = vi.fn();
const useResolveIdentityMock = vi.fn(() => ({
  mutate: mutateMock,
  isPending: false,
}));

vi.mock("@/api/matcher", () => ({
  useResolveIdentity: () => useResolveIdentityMock(),
}));

import { TitlePanel } from "./TitlePanel";

function makeClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
}

function wrapperFor(qc: QueryClient) {
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
}

function makeIdentity(overrides: Partial<QueueIdentity> = {}): QueueIdentity {
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

function makeArtist(identities: QueueIdentity[]): QueueArtist {
  return {
    id: "00000000-0000-0000-0000-000000000001",
    original_name: "Prince",
    normalized_name: "prince",
    // Resolved status is required or TitlePanel short-circuits.
    match_status: "manual_matched",
    triage_bucket: "quick_review",
    candidates: [],
    identities,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  // Default hook return — overridden per-test for the pending case.
  useResolveIdentityMock.mockReturnValue({ mutate: mutateMock, isPending: false });
});

function makeProposedMatch(overrides: Partial<ProposedMatch> = {}): ProposedMatch {
  return {
    library_file_id: "00000000-0000-0000-0000-0000000000aa",
    file_path: "/music/prince/two-skins.flac",
    track_title: "Two Skins",
    release_title: "Decoded",
    recording_mbid: "rec-mbid-0001",
    candidate_match_tier: "musicbrainz_id_search",
    ...overrides,
  };
}

describe("TitlePanel tier + score formatting", () => {
  it('shows both tier and score joined on "·" when both are present', () => {
    render(
      <TitlePanel
        artist={makeArtist([
          makeIdentity({
            match_tier: "musicbrainz_id_exact",
            confidence_score: 92,
          }),
        ])}
        onFileSearch={vi.fn()}
      />,
      { wrapper: wrapperFor(makeClient()) }
    );
    expect(screen.getByText(/Tier: musicbrainz_id_exact · Score: 92%/)).toBeDefined();
  });

  it("shows score only, no leading separator, when tier is absent", () => {
    // Regression: an earlier implementation rendered " · Score: 92%" with a
    // leading separator when match_tier was null.
    render(
      <TitlePanel
        artist={makeArtist([makeIdentity({ match_tier: null, confidence_score: 92 })])}
        onFileSearch={vi.fn()}
      />,
      { wrapper: wrapperFor(makeClient()) }
    );
    const line = screen.getByText(/Score: 92%/);
    expect(line.textContent?.trim().startsWith("·")).toBe(false);
    expect(line.textContent).toBe("Score: 92%");
  });

  it("shows tier only when confidence_score is absent", () => {
    render(
      <TitlePanel
        artist={makeArtist([
          makeIdentity({
            match_tier: "musicbrainz_id_exact",
            confidence_score: null,
          }),
        ])}
        onFileSearch={vi.fn()}
      />,
      { wrapper: wrapperFor(makeClient()) }
    );
    expect(screen.getByText("Tier: musicbrainz_id_exact")).toBeDefined();
  });

  it("renders no tier/score line when both are absent", () => {
    const { container } = render(
      <TitlePanel
        artist={makeArtist([makeIdentity({ match_tier: null, confidence_score: null })])}
        onFileSearch={vi.fn()}
      />,
      { wrapper: wrapperFor(makeClient()) }
    );
    expect(container.textContent).not.toContain("Tier:");
    expect(container.textContent).not.toContain("Score:");
  });
});

describe("TitlePanel reason badge", () => {
  it("prefers reason_detail over reason_code", () => {
    render(
      <TitlePanel
        artist={makeArtist([
          makeIdentity({
            reason_detail: "Score 72% — below confidence threshold",
            reason_code: "LOW_CONFIDENCE",
          }),
        ])}
        onFileSearch={vi.fn()}
      />,
      { wrapper: wrapperFor(makeClient()) }
    );
    expect(screen.getByText(/Score 72% — below confidence threshold/)).toBeDefined();
    // The raw code should not appear when the detail is present.
    expect(screen.queryByText("LOW_CONFIDENCE")).toBeNull();
  });

  it("falls back to reason_code when reason_detail is empty", () => {
    render(
      <TitlePanel
        artist={makeArtist([
          makeIdentity({
            reason_detail: "",
            reason_code: "NO_LOCAL_FILES",
          }),
        ])}
        onFileSearch={vi.fn()}
      />,
      { wrapper: wrapperFor(makeClient()) }
    );
    expect(screen.getByText(/NO_LOCAL_FILES/)).toBeDefined();
  });
});

describe("TitlePanel artist gate", () => {
  it("shows placeholder when artist is null", () => {
    render(<TitlePanel artist={null} onFileSearch={vi.fn()} />, {
      wrapper: wrapperFor(makeClient()),
    });
    expect(screen.getByText(/Select an artist/i)).toBeDefined();
  });

  it('shows "resolve artist first" when artist is unresolved', () => {
    const artist = makeArtist([]);
    artist.match_status = "needs_review";
    render(<TitlePanel artist={artist} onFileSearch={vi.fn()} />, {
      wrapper: wrapperFor(makeClient()),
    });
    expect(screen.getByText(/Resolve the artist first/i)).toBeDefined();
  });

  it("renders title rows when artist is auto_matched", () => {
    // Regression for PR #46: the broadened queue CTE now surfaces
    // auto_matched parents that have review-needing children. TitlePanel must
    // recognise auto_matched as resolved or the curator sees only "Resolve
    // the artist first" and can't reach the children — defeating the
    // visibility fix entirely.
    const artist = makeArtist([
      makeIdentity({
        original_title: "Your Disease",
        match_status: "needs_review",
        match_tier: "musicbrainz_id_search",
      }),
    ]);
    artist.match_status = "auto_matched";
    render(<TitlePanel artist={artist} onFileSearch={vi.fn()} />, {
      wrapper: wrapperFor(makeClient()),
    });
    expect(screen.getByText("Your Disease")).toBeDefined();
    expect(screen.getByRole("button", { name: /Find File/i })).toBeDefined();
    expect(screen.getByRole("button", { name: /Reject/i })).toBeDefined();
    expect(screen.queryByText(/Resolve the artist first/i)).toBeNull();
  });
});

describe("TitlePanel proposed match + Approve", () => {
  it("renders the proposed match block with track, release, tier, and path", () => {
    render(
      <TitlePanel
        artist={makeArtist([
          makeIdentity({
            confidence_score: 78,
            match_tier: "musicbrainz_id_search",
            proposed_match: makeProposedMatch(),
          }),
        ])}
        onFileSearch={vi.fn()}
      />,
      { wrapper: wrapperFor(makeClient()) }
    );
    const block = screen.getByTestId("proposed-match");
    expect(block.textContent).toContain("Two Skins");
    expect(block.textContent).toContain("Decoded");
    expect(block.textContent).toContain("musicbrainz_id_search");
    expect(block.textContent).toContain("/music/prince/two-skins.flac");
  });

  it("falls back to the file basename when track_title is null", () => {
    render(
      <TitlePanel
        artist={makeArtist([
          makeIdentity({
            proposed_match: makeProposedMatch({
              track_title: null,
              release_title: null,
              file_path: "/music/unknown/abcd1234.flac",
            }),
          }),
        ])}
        onFileSearch={vi.fn()}
      />,
      { wrapper: wrapperFor(makeClient()) }
    );
    const block = screen.getByTestId("proposed-match");
    expect(block.textContent).toContain("abcd1234.flac");
  });

  it("clicking Approve calls resolveIdentity with manual_matched + library_file_id", () => {
    const identity = makeIdentity({
      proposed_match: makeProposedMatch({
        library_file_id: "00000000-0000-0000-0000-0000000000bb",
      }),
    });
    render(
      <TitlePanel artist={makeArtist([identity])} onFileSearch={vi.fn()} />,
      { wrapper: wrapperFor(makeClient()) }
    );
    fireEvent.click(screen.getByRole("button", { name: /Approve/i }));
    expect(mutateMock).toHaveBeenCalledTimes(1);
    expect(mutateMock).toHaveBeenCalledWith({
      id: identity.id,
      resolution: {
        match_status: "manual_matched",
        library_file_id: "00000000-0000-0000-0000-0000000000bb",
      },
    });
  });

  it("does not render Approve when proposed_match is null", () => {
    render(
      <TitlePanel
        artist={makeArtist([makeIdentity({ proposed_match: null })])}
        onFileSearch={vi.fn()}
      />,
      { wrapper: wrapperFor(makeClient()) }
    );
    expect(screen.queryByRole("button", { name: /Approve/i })).toBeNull();
    // Find File / Reject still present.
    expect(screen.getByRole("button", { name: /Find File/i })).toBeDefined();
    expect(screen.getByRole("button", { name: /Reject/i })).toBeDefined();
  });

  it("does not render Approve when status is not needs_review even if proposed_match exists", () => {
    // A child surfaced under a resolved parent (PR #46) that has already been
    // manual_matched should not re-expose Approve — that would be a no-op /
    // confusing state. Reject + Find File stay in case the curator wants to
    // change their mind, matching today's behavior.
    render(
      <TitlePanel
        artist={makeArtist([
          makeIdentity({
            match_status: "manual_matched",
            proposed_match: makeProposedMatch(),
          }),
        ])}
        onFileSearch={vi.fn()}
      />,
      { wrapper: wrapperFor(makeClient()) }
    );
    expect(screen.queryByRole("button", { name: /Approve/i })).toBeNull();
  });

  it("disables Approve, Find File, and Reject when resolveIdentity is pending", () => {
    useResolveIdentityMock.mockReturnValue({ mutate: mutateMock, isPending: true });
    const onFileSearch = vi.fn();
    render(
      <TitlePanel
        artist={makeArtist([makeIdentity({ proposed_match: makeProposedMatch() })])}
        onFileSearch={onFileSearch}
      />,
      { wrapper: wrapperFor(makeClient()) }
    );
    const approve = screen.getByRole("button", { name: /Approve/i });
    const findFile = screen.getByRole("button", { name: /Find File/i });
    const reject = screen.getByRole("button", { name: /Reject/i });
    expect((approve as HTMLButtonElement).disabled).toBe(true);
    expect((findFile as HTMLButtonElement).disabled).toBe(true);
    expect((reject as HTMLButtonElement).disabled).toBe(true);

    // Sanity: clicking a disabled button shouldn't fire the handler.
    fireEvent.click(approve);
    fireEvent.click(findFile);
    fireEvent.click(reject);
    expect(mutateMock).not.toHaveBeenCalled();
    expect(onFileSearch).not.toHaveBeenCalled();
  });
});
