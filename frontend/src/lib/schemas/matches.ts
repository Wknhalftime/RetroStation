import { z } from 'zod'

export const MatchSchema = z.object({})
export type Match = z.infer<typeof MatchSchema>

// ---------------------------------------------------------------------------
// Match Candidate
// ---------------------------------------------------------------------------

export const MatchCandidateSchema = z.object({
  mbid: z.string(),
  name: z.string(),
  score: z.number(),
  disambiguation: z.string().optional(),
})

export type MatchCandidate = z.infer<typeof MatchCandidateSchema>
