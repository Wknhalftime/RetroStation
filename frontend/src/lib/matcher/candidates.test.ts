import { describe, expect, it } from "vitest";
import { firstCandidateMbid } from "./candidates";
import type { QueueArtist, SuggestedLink } from "@/lib/schemas/matcher";

function artistWithCandidates(candidates: SuggestedLink[]): QueueArtist {
  return {
    id: "00000000-0000-0000-0000-000000000001",
    original_name: "Prince",
    normalized_name: "prince",
    match_status: "needs_review",
    triage_bucket: "blocked",
    candidates,
    identities: [],
  };
}

function suggestion(artistId: string, mbid: string | null): SuggestedLink {
  return {
    artist_id: artistId,
    mbid,
    name: "Prince",
    disambiguation: null,
    score: 90,
    reason: "Close name in the library",
  };
}

describe("firstCandidateMbid", () => {
  it("returns null for a null artist", () => {
    expect(firstCandidateMbid(null)).toBeNull();
  });

  it("returns null when there is no candidate", () => {
    expect(firstCandidateMbid(artistWithCandidates([]))).toBeNull();
  });

  it("returns null when the first candidate is a local artist with no MBID", () => {
    expect(
      firstCandidateMbid(
        artistWithCandidates([suggestion("artist-local", null), suggestion("artist-mb", "mbid")])
      )
    ).toBeNull();
  });

  it("returns the first candidate's MBID", () => {
    expect(
      firstCandidateMbid(
        artistWithCandidates([
          suggestion("artist-prince", "mbid-prince-xxx"),
          suggestion("artist-other", "mbid-other"),
        ])
      )
    ).toBe("mbid-prince-xxx");
  });
});
