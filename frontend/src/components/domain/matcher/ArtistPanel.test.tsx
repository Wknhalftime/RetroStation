// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import type { QueueArtist } from "@/lib/schemas/matcher";

vi.mock("@/api/client", () => ({
  apiFetch: vi.fn(),
}));

import { apiFetch } from "@/api/client";
import { ArtistPanel } from "./ArtistPanel";

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

const mockedApiFetch = vi.mocked(apiFetch);

beforeEach(() => {
  mockedApiFetch.mockReset();
});

function makeArtist(overrides: Partial<QueueArtist> = {}): QueueArtist {
  return {
    id: "00000000-0000-0000-0000-000000000001",
    original_name: "Prince",
    normalized_name: "prince",
    match_status: "needs_review",
    triage_bucket: "blocked",
    candidates: [],
    identities: [],
    ...overrides,
  };
}

describe("ArtistPanel reason badge", () => {
  it("renders reason_detail when present", () => {
    render(
      <ArtistPanel
        artist={makeArtist({
          reason_detail: "Score 65% — below confidence threshold",
          reason_code: "LOW_CONFIDENCE",
        })}
      />,
      { wrapper: wrapperFor(makeClient()) }
    );
    expect(screen.getByText(/Score 65% — below confidence threshold/)).toBeDefined();
  });

  it("falls back to reason_code when reason_detail is empty string", () => {
    // Guards the mixed `||` / `??` bug from PR #25 round 2: previously an
    // empty-string detail rendered as a blank amber line.
    render(
      <ArtistPanel
        artist={makeArtist({
          reason_detail: "",
          reason_code: "NO_CANDIDATES",
        })}
      />,
      { wrapper: wrapperFor(makeClient()) }
    );
    expect(screen.getByText(/NO_CANDIDATES/)).toBeDefined();
  });

  it("renders nothing when both reason fields are absent", () => {
    const { container } = render(<ArtistPanel artist={makeArtist()} />, {
      wrapper: wrapperFor(makeClient()),
    });
    // No amber warning paragraph
    expect(container.querySelector(".text-amber-600")).toBeNull();
  });
});

describe("ArtistPanel empty-state CTA", () => {
  it('shows "Search MusicBrainz" button when candidates is empty and handler is provided', () => {
    const onSearchMusicBrainz = vi.fn();
    render(
      <ArtistPanel
        artist={makeArtist({ candidates: [] })}
        onSearchMusicBrainz={onSearchMusicBrainz}
      />,
      { wrapper: wrapperFor(makeClient()) }
    );
    const button = screen.getByRole("button", { name: /Search MusicBrainz/i });
    expect(button).toBeDefined();
    fireEvent.click(button);
    expect(onSearchMusicBrainz).toHaveBeenCalledTimes(1);
  });

  it('hides "Search MusicBrainz" button when handler is not provided', () => {
    render(<ArtistPanel artist={makeArtist({ candidates: [] })} />, {
      wrapper: wrapperFor(makeClient()),
    });
    expect(screen.queryByRole("button", { name: /Search MusicBrainz/i })).toBeNull();
  });
});

describe("ArtistPanel Search Library button", () => {
  it("shows and fires the handler when provided and candidates is empty", () => {
    const onSearchLibrary = vi.fn();
    render(
      <ArtistPanel
        artist={makeArtist({ candidates: [] })}
        onSearchLibrary={onSearchLibrary}
      />,
      { wrapper: wrapperFor(makeClient()) }
    );
    const button = screen.getByRole("button", { name: /Search Library/i });
    expect(button).toBeDefined();
    fireEvent.click(button);
    expect(onSearchLibrary).toHaveBeenCalledTimes(1);
  });

  it("is visible even when auto-candidates already exist (always-visible requirement)", () => {
    // The button must not be gated on the empty-candidates state — curators
    // may want to override a poor auto-suggestion with a known library match.
    const onSearchLibrary = vi.fn();
    render(
      <ArtistPanel
        artist={makeArtist({
          candidates: [{ mbid: "mbid-1", name: "Some Candidate", score: 70 }],
        })}
        onSearchLibrary={onSearchLibrary}
      />,
      { wrapper: wrapperFor(makeClient()) }
    );
    expect(screen.getByRole("button", { name: /Search Library/i })).toBeDefined();
  });

  it("is hidden when handler is not provided", () => {
    render(<ArtistPanel artist={makeArtist({ candidates: [] })} />, {
      wrapper: wrapperFor(makeClient()),
    });
    expect(screen.queryByRole("button", { name: /Search Library/i })).toBeNull();
  });

  it('shows "No candidates found automatically" only in empty-candidates state', () => {
    const { rerender } = render(
      <ArtistPanel artist={makeArtist({ candidates: [] })} />,
      { wrapper: wrapperFor(makeClient()) }
    );
    expect(screen.getByText(/No candidates found automatically/i)).toBeDefined();

    rerender(
      <QueryClientProvider client={makeClient()}>
        <ArtistPanel
          artist={makeArtist({
            candidates: [{ mbid: "mbid-1", name: "Some Candidate", score: 70 }],
          })}
        />
      </QueryClientProvider>
    );
    expect(screen.queryByText(/No candidates found automatically/i)).toBeNull();
  });
});
