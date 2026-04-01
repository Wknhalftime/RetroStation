import { z } from 'zod'

export const MatchSchema = z.object({})
export const MatchCandidateSchema = z.object({})

export type Match = z.infer<typeof MatchSchema>
export type MatchCandidate = z.infer<typeof MatchCandidateSchema>
