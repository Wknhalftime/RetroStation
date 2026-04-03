import { z } from 'zod'

export const ArtistSummarySchema = z.object({
  id: z.string(),
  name: z.string(),
  sort_name: z.string(),
  disambiguation: z.string().nullable(),
  work_count: z.number(),
  file_count: z.number(),
})

export const PaginatedArtistsSchema = z.object({
  items: z.array(ArtistSummarySchema),
  total: z.number(),
})

export const WorkSummarySchema = z.object({
  id: z.string(),
  title: z.string(),
  recording_count: z.number(),
  has_master: z.boolean(),
})

export const ArtistDetailSchema = z.object({
  id: z.string(),
  name: z.string(),
  sort_name: z.string(),
  disambiguation: z.string().nullable(),
  works: z.array(WorkSummarySchema),
})

export type ArtistSummary = z.infer<typeof ArtistSummarySchema>
export type PaginatedArtists = z.infer<typeof PaginatedArtistsSchema>
export type WorkSummary = z.infer<typeof WorkSummarySchema>
export type ArtistDetail = z.infer<typeof ArtistDetailSchema>

// Legacy exports preserved for any existing consumers
export const ArtistSchema = ArtistSummarySchema
export const ArtistSearchResultSchema = ArtistSummarySchema
export type Artist = ArtistSummary
export type ArtistSearchResult = ArtistSummary
