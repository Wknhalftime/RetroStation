import { z } from 'zod'

// ---------------------------------------------------------------------------
// Queue Identity
// ---------------------------------------------------------------------------

export const QueueIdentitySchema = z.object({
  id: z.string().uuid(),
  original_title: z.string(),
  normalized_title: z.string(),
  match_status: z.string(),
  match_tier: z.string().nullable(),
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
  match_status: z.enum(['MANUAL_MATCHED', 'MANUAL_REJECTED']),
  target_artist_id: z.string().nullable().optional(),
})

export type ArtistResolution = z.infer<typeof ArtistResolutionSchema>

// ---------------------------------------------------------------------------
// Identity Resolution
// ---------------------------------------------------------------------------

export const IdentityResolutionSchema = z.object({
  match_status: z.enum(['MANUAL_MATCHED', 'MANUAL_REJECTED']),
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
