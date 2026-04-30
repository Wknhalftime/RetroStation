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

const unmatchMutateMock = vi.fn();
const useUnmatchIdentityMock = vi.fn(() => ({
  mutate: unmatchMutateMock,
  isPending: false,
}));

vi.mock("@/api/matcher", () => ({
  useResolveIdentity: () => useResolveIdentityMock(),
  useUnmatchIdentity: () => useUnmatchIdentityMock(),
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

// Distinct ids per row so each card is a unique React key when several
// identities share an artist (the redesign's partition / sort tests need this).
function makeIdentityWithId(idSuffix: string, overrides: Partial<QueueIdentity> = {}): QueueIdentity {
  return makeIdentity({ id: `00000000-0000-0000-0000-0000000000${idSuffix}`, ...overrides });
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
  useUnmatchIdentityMock.mockReturnValue({
    mutate: unmatchMutateMock,
    isPending: false,
  });
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

  it("hides Approve on a needs_review card whose triage_bucket is blocked", () => {
    // Blocked items have nothing useful to one-click-approve; the proposed_match,
    // if any, is by definition low-signal — don't tempt the curator into an
    // accidental approve. Find File / Reject still work.
    render(
      <TitlePanel
        artist={makeArtist([
          makeIdentity({
            triage_bucket: "blocked",
            proposed_match: makeProposedMatch(),
          }),
        ])}
        onFileSearch={vi.fn()}
      />,
      { wrapper: wrapperFor(makeClient()) }
    );
    expect(screen.queryByRole("button", { name: /^Approve$/i })).toBeNull();
    expect(screen.getByRole("button", { name: /Find File/i })).toBeDefined();
    expect(screen.getByRole("button", { name: /^Reject$/i })).toBeDefined();
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

describe("TitlePanel sectioning + sorting", () => {
  beforeEach(() => {
    // Each test starts with no persisted disclosure preference so default
    // collapsed-resolved behaviour is deterministic.
    window.localStorage.clear();
  });

  it("partitions identities into Needs Review and Resolved sections with counts", () => {
    const identities = [
      makeIdentityWithId("01", { match_status: "needs_review", original_title: "A — review" }),
      makeIdentityWithId("02", { match_status: "auto_matched", original_title: "B — auto" }),
      makeIdentityWithId("03", { match_status: "manual_matched", original_title: "C — manual" }),
      makeIdentityWithId("04", { match_status: "auto_rejected", original_title: "D — auto-rej" }),
      makeIdentityWithId("05", { match_status: "pending", original_title: "E — pending" }),
    ];
    render(
      <TitlePanel artist={makeArtist(identities)} onFileSearch={vi.fn()} />,
      { wrapper: wrapperFor(makeClient()) }
    );
    // Section headers carry a parenthesised count of items in their group.
    expect(screen.getByRole("heading", { name: /Needs Review \(2\)/i })).toBeDefined();
    expect(screen.getByRole("heading", { name: /Resolved \(3\)/i })).toBeDefined();
  });

  it("Resolved section is collapsed by default and toggles aria-expanded on click", () => {
    const identities = [
      makeIdentityWithId("01", { match_status: "needs_review", original_title: "Review me" }),
      makeIdentityWithId("02", { match_status: "auto_matched", original_title: "Already done" }),
    ];
    render(
      <TitlePanel artist={makeArtist(identities)} onFileSearch={vi.fn()} />,
      { wrapper: wrapperFor(makeClient()) }
    );
    const disclosure = screen.getByRole("button", { name: /Resolved/i });
    expect(disclosure.getAttribute("aria-expanded")).toBe("false");
    // Body row not in document while collapsed.
    expect(screen.queryByText("Already done")).toBeNull();
    fireEvent.click(disclosure);
    expect(disclosure.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByText("Already done")).toBeDefined();
  });

  it("persists the resolved-section collapse state to localStorage", () => {
    const identities = [
      makeIdentityWithId("01", { match_status: "needs_review" }),
      makeIdentityWithId("02", { match_status: "auto_matched" }),
    ];
    const { unmount } = render(
      <TitlePanel artist={makeArtist(identities)} onFileSearch={vi.fn()} />,
      { wrapper: wrapperFor(makeClient()) }
    );
    fireEvent.click(screen.getByRole("button", { name: /Resolved/i }));
    expect(window.localStorage.getItem("matcher.resolved.collapsed")).toBe("false");
    unmount();
    // Remount: state should rehydrate as expanded.
    render(
      <TitlePanel artist={makeArtist(identities)} onFileSearch={vi.fn()} />,
      { wrapper: wrapperFor(makeClient()) }
    );
    expect(
      screen.getByRole("button", { name: /Resolved/i }).getAttribute("aria-expanded")
    ).toBe("true");
  });

  it("sorts Needs Review by triage_bucket priority then confidence DESC", () => {
    const identities = [
      makeIdentityWithId("01", {
        original_title: "Blocked-low",
        triage_bucket: "blocked",
        confidence_score: 30,
      }),
      makeIdentityWithId("02", {
        original_title: "Quick-92",
        triage_bucket: "quick_review",
        confidence_score: 92,
      }),
      makeIdentityWithId("03", {
        original_title: "Quick-70",
        triage_bucket: "quick_review",
        confidence_score: 70,
      }),
      makeIdentityWithId("04", {
        original_title: "Attention-60",
        triage_bucket: "needs_attention",
        confidence_score: 60,
      }),
      makeIdentityWithId("05", {
        original_title: "Pending-row",
        match_status: "pending",
        triage_bucket: "blocked",
        confidence_score: null,
      }),
    ];
    render(
      <TitlePanel artist={makeArtist(identities)} onFileSearch={vi.fn()} />,
      { wrapper: wrapperFor(makeClient()) }
    );
    // jsdom doesn't lay out, so we assert DOM source order via
    // compareDocumentPosition: each subsequent title must follow the previous.
    const expectedOrder = ["Quick-92", "Quick-70", "Attention-60", "Blocked-low", "Pending-row"];
    const nodes = expectedOrder.map((t) => screen.getByText(t));
    for (let i = 1; i < nodes.length; i++) {
      const rel = nodes[i - 1].compareDocumentPosition(nodes[i]);
      expect(rel & Node.DOCUMENT_POSITION_FOLLOWING).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    }
  });

  it("shows a 'no review needed' message when only resolved items remain", () => {
    const identities = [
      makeIdentityWithId("01", {
        match_status: "auto_matched",
        original_title: "Done one",
      }),
    ];
    render(
      <TitlePanel artist={makeArtist(identities)} onFileSearch={vi.fn()} />,
      { wrapper: wrapperFor(makeClient()) }
    );
    expect(screen.getByText(/Nothing left to review/i)).toBeDefined();
  });

  it("does not render the proposed-match block for resolved rows", () => {
    const identities = [
      makeIdentityWithId("01", {
        match_status: "auto_matched",
        original_title: "Auto match",
        proposed_match: makeProposedMatch(),
      }),
    ];
    render(
      <TitlePanel artist={makeArtist(identities)} onFileSearch={vi.fn()} />,
      { wrapper: wrapperFor(makeClient()) }
    );
    // Open the section so resolved rows are in the DOM.
    fireEvent.click(screen.getByRole("button", { name: /Resolved/i }));
    expect(screen.queryByTestId("proposed-match")).toBeNull();
  });

  it("renders the resolved-state breakdown in the section header", () => {
    const identities = [
      makeIdentityWithId("01", { match_status: "auto_matched" }),
      makeIdentityWithId("02", { match_status: "auto_matched" }),
      makeIdentityWithId("03", { match_status: "manual_matched" }),
      makeIdentityWithId("04", { match_status: "manual_rejected" }),
    ];
    render(
      <TitlePanel artist={makeArtist(identities)} onFileSearch={vi.fn()} />,
      { wrapper: wrapperFor(makeClient()) }
    );
    const header = screen.getByRole("button", { name: /Resolved/i });
    expect(header.textContent).toContain("2 auto");
    expect(header.textContent).toContain("1 manual");
    expect(header.textContent).toContain("1 rejected");
  });
});

describe("TitlePanel Unmatch action", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  it("clicking Unmatch on a resolved row fires unmatchIdentity with the row id", () => {
    const target = makeIdentityWithId("0a", {
      match_status: "manual_matched",
      original_title: "Manual hit",
    });
    render(
      <TitlePanel artist={makeArtist([target])} onFileSearch={vi.fn()} />,
      { wrapper: wrapperFor(makeClient()) }
    );
    // Resolved section is collapsed by default — expand it.
    fireEvent.click(screen.getByRole("button", { name: /Resolved/i }));

    const button = screen.getByRole("button", { name: /Unmatch Manual hit/i });
    fireEvent.click(button);

    expect(unmatchMutateMock).toHaveBeenCalledTimes(1);
    expect(unmatchMutateMock).toHaveBeenCalledWith({ id: target.id });
  });

  it("renders an Unmatch button on every resolved status (matched and rejected)", () => {
    const identities = [
      makeIdentityWithId("01", { match_status: "auto_matched", original_title: "AutoM" }),
      makeIdentityWithId("02", { match_status: "manual_matched", original_title: "ManualM" }),
      makeIdentityWithId("03", { match_status: "auto_rejected", original_title: "AutoR" }),
      makeIdentityWithId("04", { match_status: "manual_rejected", original_title: "ManualR" }),
    ];
    render(
      <TitlePanel artist={makeArtist(identities)} onFileSearch={vi.fn()} />,
      { wrapper: wrapperFor(makeClient()) }
    );
    fireEvent.click(screen.getByRole("button", { name: /Resolved/i }));

    expect(screen.getByRole("button", { name: /Unmatch AutoM/i })).toBeDefined();
    expect(screen.getByRole("button", { name: /Unmatch ManualM/i })).toBeDefined();
    expect(screen.getByRole("button", { name: /Unmatch AutoR/i })).toBeDefined();
    expect(screen.getByRole("button", { name: /Unmatch ManualR/i })).toBeDefined();
  });

  it("disables the Unmatch button while either mutation is pending", () => {
    useUnmatchIdentityMock.mockReturnValue({
      mutate: unmatchMutateMock,
      isPending: true,
    });
    const target = makeIdentityWithId("01", {
      match_status: "auto_matched",
      original_title: "While pending",
    });
    render(
      <TitlePanel artist={makeArtist([target])} onFileSearch={vi.fn()} />,
      { wrapper: wrapperFor(makeClient()) }
    );
    fireEvent.click(screen.getByRole("button", { name: /Resolved/i }));

    const button = screen.getByRole("button", { name: /Unmatch While pending/i });
    expect((button as HTMLButtonElement).disabled).toBe(true);
  });
});
