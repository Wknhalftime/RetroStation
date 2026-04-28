import { describe, expect, it } from "vitest";
import {
  MbArtistResultSchema,
  ProposedMatchSchema,
  QueueArtistSchema,
  QueueIdentitySchema,
} from "./matcher";

describe("QueueIdentitySchema", () => {
  it("accepts new fields", () => {
    const parsed = QueueIdentitySchema.parse({
      id: "00000000-0000-0000-0000-000000000001",
      original_title: "Purple Rain",
      normalized_title: "purple rain",
      match_status: "needs_review",
      match_tier: null,
      confidence_score: 72,
      triage_bucket: "quick_review",
      reason_code: "LOW_CONFIDENCE",
      reason_detail: "Score 72% — below confidence threshold",
    });
    expect(parsed.triage_bucket).toBe("quick_review");
  });

  it("rejects invalid triage_bucket", () => {
    expect(() =>
      QueueIdentitySchema.parse({
        id: "00000000-0000-0000-0000-000000000001",
        original_title: "x",
        normalized_title: "x",
        match_status: "needs_review",
        match_tier: null,
        triage_bucket: "invalid",
      })
    ).toThrow();
  });
});

describe("QueueArtistSchema", () => {
  it("accepts new fields including triage_bucket", () => {
    const parsed = QueueArtistSchema.parse({
      id: "00000000-0000-0000-0000-000000000002",
      original_name: "Prince",
      normalized_name: "prince",
      match_status: "needs_review",
      reason_code: "LOW_CONFIDENCE",
      reason_detail: "Score 65%",
      triage_bucket: "quick_review",
      candidates: [],
      identities: [],
    });
    expect(parsed.reason_code).toBe("LOW_CONFIDENCE");
  });

  it("accepts null candidates (backend returns null pre-MB-search)", () => {
    const parsed = QueueArtistSchema.parse({
      id: "00000000-0000-0000-0000-000000000003",
      original_name: "Prince",
      normalized_name: "prince",
      match_status: "pending",
      triage_bucket: "blocked",
      candidates: null,
      identities: [],
    });
    expect(parsed.candidates).toBeNull();
  });
});

describe("ProposedMatchSchema", () => {
  it("accepts a fully populated proposed match", () => {
    const parsed = ProposedMatchSchema.parse({
      library_file_id: "00000000-0000-0000-0000-0000000000aa",
      file_path: "/music/prince/two-skins.flac",
      track_title: "Two Skins",
      release_title: "Decoded",
      recording_mbid: "rec-mbid-0001",
      candidate_match_tier: "musicbrainz_id_search",
    });
    expect(parsed.library_file_id).toBe("00000000-0000-0000-0000-0000000000aa");
    expect(parsed.track_title).toBe("Two Skins");
  });

  it("accepts null catalog metadata (library_files columns are nullable)", () => {
    const parsed = ProposedMatchSchema.parse({
      library_file_id: "00000000-0000-0000-0000-0000000000aa",
      file_path: "/music/unknown.flac",
      track_title: null,
      release_title: null,
      recording_mbid: null,
      candidate_match_tier: "vector",
    });
    expect(parsed.track_title).toBeNull();
  });

  it("rejects a non-UUID library_file_id", () => {
    expect(() =>
      ProposedMatchSchema.parse({
        library_file_id: "not-a-uuid",
        file_path: "/x.flac",
        candidate_match_tier: "vector",
      })
    ).toThrow();
  });
});

describe("QueueIdentitySchema with proposed_match", () => {
  it("accepts a needs_review identity with a proposed_match", () => {
    const parsed = QueueIdentitySchema.parse({
      id: "00000000-0000-0000-0000-000000000001",
      original_title: "Two Skins",
      normalized_title: "two skins",
      match_status: "needs_review",
      match_tier: "musicbrainz_id_search",
      confidence_score: 78,
      triage_bucket: "needs_attention",
      proposed_match: {
        library_file_id: "00000000-0000-0000-0000-0000000000aa",
        file_path: "/music/two-skins.flac",
        track_title: "Two Skins",
        release_title: "Decoded",
        recording_mbid: "rec-mbid-0001",
        candidate_match_tier: "musicbrainz_id_search",
      },
    });
    expect(parsed.proposed_match?.library_file_id).toBe(
      "00000000-0000-0000-0000-0000000000aa"
    );
  });

  it("treats proposed_match as optional and nullable", () => {
    const a = QueueIdentitySchema.parse({
      id: "00000000-0000-0000-0000-000000000001",
      original_title: "x",
      normalized_title: "x",
      match_status: "pending",
      match_tier: null,
      triage_bucket: "blocked",
    });
    expect(a.proposed_match).toBeUndefined();

    const b = QueueIdentitySchema.parse({
      id: "00000000-0000-0000-0000-000000000001",
      original_title: "x",
      normalized_title: "x",
      match_status: "needs_review",
      match_tier: null,
      triage_bucket: "blocked",
      proposed_match: null,
    });
    expect(b.proposed_match).toBeNull();
  });
});

describe("MbArtistResultSchema", () => {
  it("accepts the backend shape", () => {
    const parsed = MbArtistResultSchema.parse({
      id: "mbid-1",
      name: "Prince",
      score: 100,
      disambiguation: "",
    });
    expect(parsed.id).toBe("mbid-1");
  });

  it("defaults disambiguation when missing", () => {
    const parsed = MbArtistResultSchema.parse({
      id: "mbid-1",
      name: "Prince",
      score: 100,
    });
    expect(parsed.disambiguation).toBe("");
  });
});
