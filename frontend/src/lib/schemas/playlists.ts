import { z } from "zod";

// ---------------------------------------------------------------------------
// Playlist response shapes
// ---------------------------------------------------------------------------

export const PlaylistSummarySchema = z.object({
  id: z.string().uuid(),
  name: z.string(),
  station_id: z.string().uuid().nullable(),
  content_hash: z.string(),
  ingested_at: z.string(),
  event_count: z.number(),
});
export type PlaylistSummary = z.infer<typeof PlaylistSummarySchema>;

export const PlaylistDetailSchema = z.object({
  id: z.string().uuid(),
  name: z.string(),
  station_id: z.string().uuid().nullable(),
  content_hash: z.string(),
  ingested_at: z.string(),
});
export type PlaylistDetail = z.infer<typeof PlaylistDetailSchema>;

// ---------------------------------------------------------------------------
// Event shapes
// ---------------------------------------------------------------------------

export const EventItemSchema = z.object({
  id: z.string().uuid(),
  played_at: z.string(),
  artist_name: z.string(),
  title: z.string(),
  match_status: z.string(),
  match_tier: z.string().nullable(),
});
export type EventItem = z.infer<typeof EventItemSchema>;

export const PaginatedEventsSchema = z.object({
  items: z.array(EventItemSchema),
  total: z.number(),
});
export type PaginatedEvents = z.infer<typeof PaginatedEventsSchema>;
