import { z } from 'zod'

// ---------------------------------------------------------------------------
// Triage Bucket
// ---------------------------------------------------------------------------

export const TriageBucketSchema = z.enum([
  'quick_review',
  'needs_attention',
  'blocked',
])

export type TriageBucket = z.infer<typeof TriageBucketSchema>

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
})

export type QueueIdentity = z.infer<typeof QueueIdentitySchema>

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
  candidates: z.array(z.record(z.unknown()).nullable()),
  identities: z.array(QueueIdentitySchema),
})

export type QueueArtist = z.infer<typeof QueueArtistSchema>

// ---------------------------------------------------------------------------
// Matching Queue
// ---------------------------------------------------------------------------

export const MatchingQueueSchema = z.object({
  items: z.array(QueueArtistSchema),
  total: z.number(),
})

export type MatchingQueue = z.infer<typeof MatchingQueueSchema>

// ---------------------------------------------------------------------------
// Artist Resolution
// ---------------------------------------------------------------------------

export const ArtistResolutionSchema = z.object({
  match_status: z.enum(['manual_matched', 'manual_rejected']),
  target_artist_id: z.string().nullable().optional(),
})

export type ArtistResolution = z.infer<typeof ArtistResolutionSchema>

// ---------------------------------------------------------------------------
// Identity Resolution
// ---------------------------------------------------------------------------

export const IdentityResolutionSchema = z.object({
  match_status: z.enum(['manual_matched', 'manual_rejected']),
  library_file_id: z.string().uuid().nullable().optional(),
})

export type IdentityResolution = z.infer<typeof IdentityResolutionSchema>

// ---------------------------------------------------------------------------
// Resolve Result
// ---------------------------------------------------------------------------

export const ResolveResultSchema = z.object({
  id: z.string().uuid(),
  match_status: z.string(),
})

export type ResolveResult = z.infer<typeof ResolveResultSchema>

// ---------------------------------------------------------------------------
// MusicBrainz Artist Search Result
// ---------------------------------------------------------------------------

export const MbArtistResultSchema = z.object({
  id: z.string(),
  name: z.string(),
  score: z.number(),
  disambiguation: z.string().optional().default(''),
})

export type MbArtistResult = z.infer<typeof MbArtistResultSchema>
