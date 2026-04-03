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
