// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import type { QueueArtist, QueueIdentity } from "@/lib/schemas/matcher";

vi.mock("@/api/client", () => ({
  apiFetch: vi.fn(),
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
});

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
