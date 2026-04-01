import { z } from 'zod'

export const ArtistSchema = z.object({})
export const ArtistDetailSchema = z.object({})
export const ArtistSearchResultSchema = z.object({})

export type Artist = z.infer<typeof ArtistSchema>
export type ArtistDetail = z.infer<typeof ArtistDetailSchema>
export type ArtistSearchResult = z.infer<typeof ArtistSearchResultSchema>
