import { z } from 'zod'

export const PlaylistSchema = z.object({})
export const PlaylistEventSchema = z.object({})
export const ExportResultSchema = z.object({})

export type Playlist = z.infer<typeof PlaylistSchema>
export type PlaylistEvent = z.infer<typeof PlaylistEventSchema>
export type ExportResult = z.infer<typeof ExportResultSchema>
