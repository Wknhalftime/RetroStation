// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor, within, act } from "@testing-library/react";
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

/** A suggested link as the queue sends it (phase 1c, S8). */
const SOME_CANDIDATE = {
  artist_id: "artist-1",
  mbid: "mbid-1",
  name: "Some Candidate",
  disambiguation: null,
  score: 70,
  reason: "Close name in the library",
};

// ---------------------------------------------------------------------------
// Phase 1b (SPEC S6, E2, E3, E6): Reject and Unmatch ask first, with the off-air preview
// ---------------------------------------------------------------------------

const ARTIST_URL = "/api/v1/matching/artists/00000000-0000-0000-0000-000000000001";
const PREVIEW = { off_air: { lines: 3, plays: 120 }, kept: { manual: 2, same_name: 5 } };
const ZERO = { off_air: { lines: 0, plays: 0 }, kept: { manual: 0, same_name: 0 } };

/** The action's answer: its own counts, which differ from PREVIEW (E2). */
function done(matchStatus: string) {
  return {
    id: "00000000-0000-0000-0000-000000000001",
    match_status: matchStatus,
    cascade: { off_air: { lines: 4, plays: 130 }, kept: { manual: 2, same_name: 5 } },
  };
}

/** Answer the preview GET and the action POSTs; any other call fails the test. */
function routeApi({
  preview,
  action = () => Promise.resolve(done("manual_rejected")),
}: {
  preview: () => Promise<unknown>;
  action?: () => Promise<unknown>;
}) {
  mockedApiFetch.mockImplementation(((url: string) => {
    if (url.includes("/cascade-preview")) return preview();
    if (url.endsWith("/resolve") || url.endsWith("/unmatch")) return action();
    return Promise.reject(new Error(`unexpected call ${url}`));
  }) as typeof apiFetch);
}

function actionCalls(): unknown[][] {
  return mockedApiFetch.mock.calls.filter(([url]) => !String(url).includes("/cascade-preview"));
}

function previewUrls(): string[] {
  return mockedApiFetch.mock.calls
    .map(([url]) => String(url))
    .filter((url) => url.includes("/cascade-preview"));
}

const ACTIONS = [
  {
    action: "reject",
    status: "needs_review",
    answer: "manual_rejected",
    button: /Reject Artist/i,
    url: `${ARTIST_URL}/resolve`,
    body: JSON.stringify({ match_status: "manual_rejected", target_artist_id: null }),
  },
  {
    action: "unmatch",
    status: "manual_matched",
    answer: "needs_review",
    button: /Unmatch artist/i,
    url: `${ARTIST_URL}/unmatch`,
    body: undefined,
  },
] as const;

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
          candidates: [SOME_CANDIDATE],
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
            candidates: [SOME_CANDIDATE],
          })}
        />
      </QueryClientProvider>
    );
    expect(screen.queryByText(/No candidates found automatically/i)).toBeNull();
  });
});

describe("ArtistPanel Unmatch action", () => {
  it("does not render Unmatch when artist is in pending or needs_review", () => {
    render(<ArtistPanel artist={makeArtist({ match_status: "pending" })} />, {
      wrapper: wrapperFor(makeClient()),
    });
    expect(screen.queryByRole("button", { name: /Unmatch artist/i })).toBeNull();
  });

  it.each([
    ["auto_matched"],
    ["manual_matched"],
    ["auto_rejected"],
    ["manual_rejected"],
  ] as const)(
    "renders Unmatch when artist is %s",
    (status) => {
      render(<ArtistPanel artist={makeArtist({ match_status: status })} />, {
        wrapper: wrapperFor(makeClient()),
      });
      expect(screen.getByRole("button", { name: /Unmatch artist/i })).toBeDefined();
      // M24 (phase 1b): Unmatch keeps manual and same-name songs, so it no longer says it
      // unmatches all of them.
      expect(screen.queryByText(/all songs/i)).toBeNull();
    }
  );

  it("Unmatch, once confirmed, posts to the unmatch endpoint with the artist id", async () => {
    routeApi({
      preview: () => Promise.resolve(PREVIEW),
      action: () => Promise.resolve(done("needs_review")),
    });
    render(
      <ArtistPanel artist={makeArtist({ match_status: "manual_matched" })} />,
      { wrapper: wrapperFor(makeClient()) }
    );
    fireEvent.click(screen.getByRole("button", { name: /Unmatch artist/i }));
    fireEvent.click(await screen.findByRole("button", { name: /^Confirm/i }));

    await waitFor(() => {
      expect(mockedApiFetch).toHaveBeenCalledWith(
        `${ARTIST_URL}/unmatch`,
        expect.objectContaining({ method: "POST" })
      );
    });
  });
});

describe.each(ACTIONS)("ArtistPanel $action asks first", (row) => {
  const { action, status, answer, button, url, body } = row;
  const answered = () => Promise.resolve(done(answer));

  function renderPanel(onCascadeDone = vi.fn()) {
    render(
      <ArtistPanel artist={makeArtist({ match_status: status })} onCascadeDone={onCascadeDone} />,
      { wrapper: wrapperFor(makeClient()) }
    );
    return onCascadeDone;
  }

  it("confirms_with_off_air_count: the dialog shows what goes off air and what stays", async () => {
    routeApi({ preview: () => Promise.resolve(PREVIEW), action: answered });
    renderPanel();

    fireEvent.click(screen.getByRole("button", { name: button }));

    const dialog = await screen.findByRole("alertdialog");
    await waitFor(() => {
      expect(dialog.textContent).toMatch(/3 songs \(120 plays\) go off air; 7 stay/);
    });
    expect(previewUrls()).toContain(`${ARTIST_URL}/cascade-preview?action=${action}`);
    // Nothing is done until Confirm.
    expect(actionCalls()).toEqual([]);
  });

  it("Confirm runs the action", async () => {
    routeApi({ preview: () => Promise.resolve(PREVIEW), action: answered });
    renderPanel();
    fireEvent.click(screen.getByRole("button", { name: button }));

    fireEvent.click(await screen.findByRole("button", { name: /^Confirm/i }));

    await waitFor(() => {
      expect(actionCalls()).toEqual([
        [url, body === undefined ? { method: "POST" } : { method: "POST", body }],
      ]);
    });
  });

  it("Cancel does nothing and closes the dialog", async () => {
    routeApi({ preview: () => Promise.resolve(PREVIEW), action: answered });
    renderPanel();
    fireEvent.click(screen.getByRole("button", { name: button }));
    await screen.findByRole("button", { name: /^Confirm/i });

    fireEvent.click(screen.getByRole("button", { name: /Cancel/i }));

    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(actionCalls()).toEqual([]);
  });

  it("shows a loading state and offers no Confirm until the preview arrives", async () => {
    let arrive: (value: unknown) => void = () => {};
    routeApi({ preview: () => new Promise((resolve) => (arrive = resolve)), action: answered });
    renderPanel();

    fireEvent.click(screen.getByRole("button", { name: button }));

    const dialog = await screen.findByRole("alertdialog");
    expect(dialog.textContent).toMatch(/counting/i);
    const confirm = screen.queryByRole("button", { name: /^Confirm/i });
    expect(confirm === null || (confirm as HTMLButtonElement).disabled).toBe(true);

    arrive(PREVIEW);
    const ready = await screen.findByRole("button", { name: /^Confirm/i });
    await waitFor(() => expect((ready as HTMLButtonElement).disabled).toBe(false));
  });

  it("shows_preview_error: a failed preview shows the error and offers no Confirm", async () => {
    routeApi({ preview: () => Promise.reject(new Error("API 500: boom")), action: answered });
    renderPanel();

    fireEvent.click(screen.getByRole("button", { name: button }));

    const dialog = await screen.findByRole("alertdialog");
    await waitFor(() => expect(dialog.textContent).toMatch(/could not count/i));
    expect(screen.queryByRole("button", { name: /^Confirm/i })).toBeNull();
    expect(screen.getByRole("button", { name: /Cancel/i })).toBeDefined();
    expect(actionCalls()).toEqual([]);
  });

  it("disables the buttons while the action runs, so a second press sends nothing (E3)", async () => {
    routeApi({
      preview: () => Promise.resolve(PREVIEW),
      action: () => new Promise(() => {}), // never answers
    });
    renderPanel();
    fireEvent.click(screen.getByRole("button", { name: button }));
    const confirm = await screen.findByRole("button", { name: /^Confirm/i });

    fireEvent.click(confirm);

    await waitFor(() => expect((confirm as HTMLButtonElement).disabled).toBe(true));
    expect((screen.getByRole("button", { name: /Cancel/i }) as HTMLButtonElement).disabled).toBe(
      true
    );
    fireEvent.click(confirm);
    expect(actionCalls()).toHaveLength(1);
  });

  it("reports the action's own count, not the preview's (E2)", async () => {
    routeApi({ preview: () => Promise.resolve(PREVIEW), action: answered });
    const onCascadeDone = renderPanel();
    fireEvent.click(screen.getByRole("button", { name: button }));

    fireEvent.click(await screen.findByRole("button", { name: /^Confirm/i }));

    await waitFor(() => expect(onCascadeDone).toHaveBeenCalledTimes(1));
    const message = String(onCascadeDone.mock.calls[0][0]);
    expect(message).toMatch(/4 songs \(130 plays\) went off air; 7 stayed/);
    expect(screen.queryByRole("alertdialog")).toBeNull();
  });

  it("a failed action keeps the dialog open with the error, and reports nothing", async () => {
    routeApi({
      preview: () => Promise.resolve(PREVIEW),
      action: () => Promise.reject(new Error("API 409: already unmatched")),
    });
    const onCascadeDone = renderPanel();
    fireEvent.click(screen.getByRole("button", { name: button }));

    fireEvent.click(await screen.findByRole("button", { name: /^Confirm/i }));

    const dialog = await screen.findByRole("alertdialog");
    await waitFor(() => expect(dialog.textContent).toMatch(/already unmatched/i));
    const cancel = screen.getByRole("button", { name: /Cancel/i }) as HTMLButtonElement;
    await waitFor(() => expect(cancel.disabled).toBe(false));
    expect(onCascadeDone).not.toHaveBeenCalled();
    fireEvent.click(cancel);
    expect(screen.queryByRole("alertdialog")).toBeNull();
  });

  it("E1: a name with nothing on air shows 0 / 0 and still runs the action", async () => {
    routeApi({ preview: () => Promise.resolve(ZERO), action: answered });
    renderPanel();
    fireEvent.click(screen.getByRole("button", { name: button }));

    const dialog = await screen.findByRole("alertdialog");
    await waitFor(() =>
      expect(dialog.textContent).toMatch(/0 songs \(0 plays\) go off air; 0 stay/)
    );
    const confirm = screen.getByRole("button", { name: /^Confirm/i }) as HTMLButtonElement;
    expect(confirm.disabled).toBe(false);
    fireEvent.click(confirm);

    await waitFor(() => expect(actionCalls()).toHaveLength(1));
  });
});

describe("ArtistPanel LINK_REJECTED reason", () => {
  it("names the reason in words when there is no detail", () => {
    render(<ArtistPanel artist={makeArtist({ reason_code: "LINK_REJECTED" })} />, {
      wrapper: wrapperFor(makeClient()),
    });
    expect(screen.getByText(/an artist you rejected/i)).toBeDefined();
  });
});

// ---------------------------------------------------------------------------
// Phase 1c (SPEC S8): the candidates are the name's suggested links
// ---------------------------------------------------------------------------

const SUGGESTIONS = [
  {
    artist_id: "artist-club",
    mbid: "mbid-club",
    name: "Blue Oyster Club",
    disambiguation: "Long Island bar band",
    score: 88,
    reason: "Was linked automatically by a close name; now a suggestion",
  },
  // Two local artists of one name and no MBID: only their ids tell them apart.
  {
    artist_id: "artist-local-1",
    mbid: null,
    name: "Blue Oyster",
    disambiguation: null,
    score: 81,
    reason: "Close name in the library",
  },
  {
    artist_id: "artist-local-2",
    mbid: null,
    name: "Blue Oyster",
    disambiguation: null,
    score: 81,
    reason: "Close name in the library",
  },
];

describe("ArtistPanel suggested links", () => {
  it("shows_suggested_links: each with its name, disambiguation, score and reason", () => {
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
    try {
      render(<ArtistPanel artist={makeArtist({ candidates: SUGGESTIONS })} />, {
        wrapper: wrapperFor(makeClient()),
      });

      expect(screen.getAllByRole("button", { name: /^Accept/i })).toHaveLength(3);
      expect(screen.getByText("Blue Oyster Club")).toBeDefined();
      expect(screen.getByText("Long Island bar band")).toBeDefined();
      expect(screen.getAllByText("Blue Oyster")).toHaveLength(2);
      expect(screen.getByText(/88%/)).toBeDefined();
      expect(screen.getByText(/now a suggestion/)).toBeDefined();
      expect(screen.getAllByText(/Close name in the library/)).toHaveLength(2);
      expect(screen.queryByText(/No candidates found automatically/i)).toBeNull();
      // Keyed by artist id: two suggestions without an MBID are two rows, with no key
      // warning of any kind (a clash, or a missing key).
      const keyWarnings = consoleError.mock.calls.filter((call) =>
        call.some((part) => String(part).includes("key"))
      );
      expect(keyWarnings).toEqual([]);
    } finally {
      consoleError.mockRestore();
    }
  });

  it("Accept is the manual resolve, to the suggestion's artist by its id", async () => {
    mockedApiFetch.mockResolvedValue({
      id: "00000000-0000-0000-0000-000000000001",
      match_status: "manual_matched",
    });
    render(<ArtistPanel artist={makeArtist({ candidates: SUGGESTIONS })} />, {
      wrapper: wrapperFor(makeClient()),
    });

    fireEvent.click(screen.getAllByRole("button", { name: /^Accept/i })[2]);

    await waitFor(() => {
      expect(mockedApiFetch).toHaveBeenCalledWith(`${ARTIST_URL}/resolve`, {
        method: "POST",
        body: JSON.stringify({
          match_status: "manual_matched",
          target_artist_id: "artist-local-2",
        }),
      });
    });
  });
});

// ---------------------------------------------------------------------------
// Phase 1c (SPEC S9, E4): Not an artist, with a confirmation and an Undo. The panel asks; the
// page holds the listing for its five-second Undo and sends it only then (Lance, 2026-11-03),
// so an Undo inside the five seconds has nothing to take back (MatcherBrowser.test.tsx).
// ---------------------------------------------------------------------------

const NOT_AN_ARTIST_URL = `${ARTIST_URL}/not-an-artist`;
const UNLISTED = { id: "00000000-0000-0000-0000-000000000001", listed: false };

function listCalls(): unknown[][] {
  return mockedApiFetch.mock.calls.filter(([url]) => String(url) === NOT_AN_ARTIST_URL);
}

describe("ArtistPanel Not an artist", () => {
  function renderPanel(overrides: Partial<QueueArtist> = {}, onNotAnArtist = vi.fn()) {
    render(
      <ArtistPanel
        artist={makeArtist({
          original_name: "SPOT BREAK",
          normalized_name: "spot break",
          ...overrides,
        })}
        onNotAnArtist={onNotAnArtist}
      />,
      { wrapper: wrapperFor(makeClient()) }
    );
    return onNotAnArtist;
  }

  it("not_an_artist_with_undo: asks first, and on Confirm hands the name to the page's Undo, sending nothing", async () => {
    const onNotAnArtist = renderPanel();

    fireEvent.click(screen.getByRole("button", { name: /^Not an artist$/i }));

    const dialog = await screen.findByRole("alertdialog");
    expect(dialog.textContent).toMatch(/SPOT BREAK/);
    // It says what Confirm does: the name's links are removed.
    expect(dialog.textContent).toMatch(/links/i);
    expect(onNotAnArtist).not.toHaveBeenCalled();

    fireEvent.click(within(dialog).getByRole("button", { name: /^Confirm/i }));

    expect(onNotAnArtist).toHaveBeenCalledTimes(1);
    const [artist] = onNotAnArtist.mock.calls[0] as [QueueArtist];
    expect([artist.id, artist.original_name]).toEqual([
      "00000000-0000-0000-0000-000000000001",
      "SPOT BREAK",
    ]);
    expect(screen.queryByRole("alertdialog")).toBeNull();
    // The panel writes nothing: the page sends the listing when its Undo is over.
    expect(listCalls()).toEqual([]);
  });

  it("Cancel hands nothing over and closes the dialog", async () => {
    const onNotAnArtist = renderPanel();
    fireEvent.click(screen.getByRole("button", { name: /^Not an artist$/i }));
    await screen.findByRole("alertdialog");

    fireEvent.click(screen.getByRole("button", { name: /Cancel/i }));

    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(onNotAnArtist).not.toHaveBeenCalled();
    expect(listCalls()).toEqual([]);
  });

  it("E4: Confirm pressed twice hands the name over once", async () => {
    const onNotAnArtist = renderPanel();
    fireEvent.click(screen.getByRole("button", { name: /^Not an artist$/i }));
    const dialog = await screen.findByRole("alertdialog");
    const confirm = within(dialog).getByRole("button", { name: /^Confirm/i });

    // Both presses before React re-renders, so the second still reaches the open dialog.
    act(() => {
      confirm.click();
      confirm.click();
    });

    expect(onNotAnArtist).toHaveBeenCalledTimes(1);
  });

  it("is offered for a matched name too (VARIOUS was auto_matched)", () => {
    renderPanel({ original_name: "VARIOUS", match_status: "auto_matched" });

    expect(screen.getByRole("button", { name: /^Not an artist$/i })).toBeDefined();
  });

  it("in the placeholder view offers only Back to the queue, which takes the name off", async () => {
    mockedApiFetch.mockResolvedValue(UNLISTED);
    const offers = [
      /^Not an artist$/i,
      /^Accept/i,
      /Reject Artist/i,
      /Unmatch artist/i,
      /Search Library/i,
      /Search MusicBrainz/i,
    ];
    function renderListed(overrides: Partial<QueueArtist>) {
      return render(
        <ArtistPanel
          artist={makeArtist({ original_name: "VARIOUS", normalized_name: "various", ...overrides })}
          listed
          onNotAnArtist={vi.fn()}
          onSearchLibrary={vi.fn()}
          onSearchMusicBrainz={vi.fn()}
        />,
        { wrapper: wrapperFor(makeClient()) }
      );
    }

    // A listed name is never linked (Resolve answers 409): no decision is offered but this one.
    // In review with a candidate, Accept and Reject would show; matched, Unmatch would.
    for (const overrides of [
      { match_status: "needs_review", candidates: [SOME_CANDIDATE] },
      { match_status: "auto_matched" },
    ]) {
      const view = renderListed(overrides);
      for (const name of offers) {
        expect(screen.queryByRole("button", { name })).toBeNull();
      }
      view.unmount();
    }

    renderListed({ match_status: "auto_matched" });
    fireEvent.click(screen.getByRole("button", { name: /Back to the queue/i }));

    await waitFor(() =>
      expect(listCalls()).toEqual([
        [NOT_AN_ARTIST_URL, expect.objectContaining({ method: "DELETE" })],
      ])
    );
  });
});
