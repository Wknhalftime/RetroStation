import { z } from "zod";

// ---------------------------------------------------------------------------
// Response shapes (from API)
// ---------------------------------------------------------------------------

export const StationResponseSchema = z.object({
  id: z.string().uuid(),
  call_letters: z.string(),
  name: z.string().nullable(),
  city: z.string().nullable(),
  format_name: z.string().nullable(),
  created_at: z.string(),
});
export type StationResponse = z.infer<typeof StationResponseSchema>;

export const StationSummarySchema = StationResponseSchema.extend({
  playlist_count: z.number(),
});
export type StationSummary = z.infer<typeof StationSummarySchema>;

export const StationListSchema = z.array(StationSummarySchema);
export type StationList = z.infer<typeof StationListSchema>;

// ---------------------------------------------------------------------------
// Mutation payloads
// ---------------------------------------------------------------------------

export const StationCreateSchema = z.object({
  call_letters: z.string().min(1),
  name: z.string().nullable().optional(),
  city: z.string().nullable().optional(),
  format_name: z.string().nullable().optional(),
});
export type StationCreate = z.infer<typeof StationCreateSchema>;

export const StationUpdateSchema = z.object({
  call_letters: z.string().min(1).optional(),
  name: z.string().nullable().optional(),
  city: z.string().nullable().optional(),
  format_name: z.string().nullable().optional(),
});
export type StationUpdate = z.infer<typeof StationUpdateSchema>;

// ---------------------------------------------------------------------------
// Station event shapes (calendar zoom feature)
// ---------------------------------------------------------------------------

export const StationEventItemSchema = z.object({
  id: z.string().uuid(),
  played_at: z.string(),
  artist_name: z.string(),
  title: z.string(),
  match_status: z.string(),
  match_tier: z.string().nullable(),
  playlist_name: z.string(),
});
export type StationEventItem = z.infer<typeof StationEventItemSchema>;

export const StationPaginatedEventsSchema = z.object({
  items: z.array(StationEventItemSchema),
  total: z.number(),
});
export type StationPaginatedEvents = z.infer<typeof StationPaginatedEventsSchema>;

// ---------------------------------------------------------------------------
// Missing Matches report shapes
// ---------------------------------------------------------------------------

export const MissingMatchItemSchema = z.object({
  identity_id: z.string().uuid(),
  artist_name: z.string(),
  track_title: z.string(),
  track_status: z.string(),
  play_count: z.number(),
  impact_pct: z.number(),
});
export type MissingMatchItem = z.infer<typeof MissingMatchItemSchema>;

export const PaginatedMissingMatchesSchema = z.object({
  items: z.array(MissingMatchItemSchema),
  total: z.number(),
});
export type PaginatedMissingMatches = z.infer<typeof PaginatedMissingMatchesSchema>;
