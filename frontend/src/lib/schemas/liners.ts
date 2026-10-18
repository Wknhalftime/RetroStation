import { z } from "zod";
import { ClipFormatSchema } from "@/lib/schemas/streaming";

// ---------------------------------------------------------------------------
// GET /api/v1/stations/{station_id}/liners (and the PUT .../liners/cadence answer)
// ---------------------------------------------------------------------------

export const LinerCueStateSchema = z.enum(["edited", "auto", "failed", "analysing", "silent"]);

export type LinerCueState = z.infer<typeof LinerCueStateSchema>;

export const LinerSchema = z.object({
  id: z.string(),
  name: z.string(),
  format: ClipFormatSchema,
  duration_ms: z.number(),
  cues: LinerCueStateSchema,
  uploaded_at: z.string(),
});

export type Liner = z.infer<typeof LinerSchema>;

export const LinerLibrarySchema = z.object({
  songs_between_liners: z.number().int().nullable(),
  liners: z.array(LinerSchema),
});

export type LinerLibrary = z.infer<typeof LinerLibrarySchema>;

// ---------------------------------------------------------------------------
// POST /api/v1/stations/{station_id}/liners
// ---------------------------------------------------------------------------

export const LinerAddedSchema = z.object({
  id: z.string(),
  outcome: z.enum(["added", "duplicate"]),
});

export type LinerAdded = z.infer<typeof LinerAddedSchema>;
