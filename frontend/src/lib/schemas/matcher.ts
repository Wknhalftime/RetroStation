import { z } from "zod";

// ---------------------------------------------------------------------------
// Triage Bucket
// ---------------------------------------------------------------------------

export const TriageBucketSchema = z.enum(["quick_review", "needs_attention", "blocked"]);

export type TriageBucket = z.infer<typeof TriageBucketSchema>;

// ---------------------------------------------------------------------------
// Proposed Match
// ---------------------------------------------------------------------------

// Best-scoring library_file candidate the matcher chose for a needs_review
// identity. Surfaced on the card so a curator can approve in one click.
//
// `library_file_id` and `file_path` are required because the UI binds Approve
// against the FK and renders a path fallback when track/release are unknown.
// `track_title`, `release_title`, and `recording_mbid` are nullable on the wire
// because the corresponding library_files columns are nullable.
export const ProposedMatchSchema = z.object({
  library_file_id: z.string().uuid(),
  file_path: z.string(),
  track_title: z.string().nullable().optional(),
  release_title: z.string().nullable().optional(),
  recording_mbid: z.string().nullable().optional(),
  candidate_match_tier: z.string(),
});

export type ProposedMatch = z.infer<typeof ProposedMatchSchema>;

// ---------------------------------------------------------------------------
// Queue Identity
// ---------------------------------------------------------------------------

export const QueueIdentitySchema = z.object({
  id: z.string().uuid(),
  original_title: z.string(),
  normalized_title: z.string(),
  match_status: z.string(),
  match_tier: z.string().nullable(),
  confidence_score: z.number().nullable().optional(),
  triage_bucket: TriageBucketSchema,
  reason_code: z.string().nullable().optional(),
  reason_detail: z.string().nullable().optional(),
  proposed_match: ProposedMatchSchema.nullable().optional(),
});

export type QueueIdentity = z.infer<typeof QueueIdentitySchema>;

// ---------------------------------------------------------------------------
// Queue Artist
// ---------------------------------------------------------------------------

export const QueueArtistSchema = z.object({
  id: z.string().uuid(),
  original_name: z.string(),
  normalized_name: z.string(),
  match_status: z.string(),
  reason_code: z.string().nullable().optional(),
  reason_detail: z.string().nullable().optional(),
  triage_bucket: TriageBucketSchema,
  // Backend returns `candidates: list[...] | None`; null arrives as `null`
  // on the wire when no MB search has been run yet. Accept both the null
  // and the array form so schema parsing doesn't reject legitimate rows.
  candidates: z.array(z.record(z.unknown()).nullable()).nullable(),
  identities: z.array(QueueIdentitySchema),
});

export type QueueArtist = z.infer<typeof QueueArtistSchema>;

// ---------------------------------------------------------------------------
// Matching Queue
// ---------------------------------------------------------------------------

export const MatchingQueueSchema = z.object({
  items: z.array(QueueArtistSchema),
  total: z.number(),
});

export type MatchingQueue = z.infer<typeof MatchingQueueSchema>;

// ---------------------------------------------------------------------------
// Artist Resolution
// ---------------------------------------------------------------------------

export const ArtistResolutionSchema = z.object({
  match_status: z.enum(["manual_matched", "manual_rejected"]),
  target_artist_id: z.string().nullable().optional(),
});

export type ArtistResolution = z.infer<typeof ArtistResolutionSchema>;

// ---------------------------------------------------------------------------
// Identity Resolution
// ---------------------------------------------------------------------------

export const IdentityResolutionSchema = z.object({
  match_status: z.enum(["manual_matched", "manual_rejected"]),
  library_file_id: z.string().uuid().nullable().optional(),
});

export type IdentityResolution = z.infer<typeof IdentityResolutionSchema>;

// ---------------------------------------------------------------------------
// Resolve Result
// ---------------------------------------------------------------------------

export const ResolveResultSchema = z.object({
  id: z.string().uuid(),
  match_status: z.string(),
});

export type ResolveResult = z.infer<typeof ResolveResultSchema>;

// ---------------------------------------------------------------------------
// MusicBrainz Artist Search Result
// ---------------------------------------------------------------------------

export const MbArtistResultSchema = z.object({
  id: z.string(),
  name: z.string(),
  score: z.number(),
  disambiguation: z.string().optional().default(""),
});

export type MbArtistResult = z.infer<typeof MbArtistResultSchema>;

// Envelope returned by GET /api/v1/matching/mb-artists. Defaults `items` to
// `[]` so a malformed response (missing field, null) never yields undefined
// at the call site — critical because `useQuery<MbArtistResult[]>` typing
// promises an array.
export const MbArtistSearchResponseSchema = z.object({
  items: z.array(MbArtistResultSchema).default([]),
});

export type MbArtistSearchResponse = z.infer<typeof MbArtistSearchResponseSchema>;
