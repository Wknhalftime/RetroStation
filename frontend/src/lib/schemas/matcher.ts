import { z } from 'zod'

export const MatcherQueueItemSchema = z.object({})
export const ArtistResolutionSchema = z.object({})

export type MatcherQueueItem = z.infer<typeof MatcherQueueItemSchema>
export type ArtistResolution = z.infer<typeof ArtistResolutionSchema>
