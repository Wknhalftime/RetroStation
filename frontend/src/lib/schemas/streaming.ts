import { z } from "zod";

// ---------------------------------------------------------------------------
// Sign-off clip
// ---------------------------------------------------------------------------

export const ClipFormatSchema = z.enum(["flac", "mp3", "wav"]);

export type ClipFormat = z.infer<typeof ClipFormatSchema>;

export const SignOffSchema = z.object({
  name: z.string(),
  seconds: z.number(),
  format: ClipFormatSchema,
});

export type SignOff = z.infer<typeof SignOffSchema>;

// ---------------------------------------------------------------------------
// Streaming state
// ---------------------------------------------------------------------------

export const StreamStateSchema = z.enum(["on", "off", "unavailable"]);

export type StreamState = z.infer<typeof StreamStateSchema>;

// ---------------------------------------------------------------------------
// GET /api/v1/streaming/settings
// ---------------------------------------------------------------------------

export const StreamSettingsSchema = z.object({
  streaming: StreamStateSchema,
  max_sessions: z.number().int().positive().nullable(),
  max_sessions_problem: z.string().nullable(),
  sign_off: SignOffSchema.nullable(),
  sign_off_problem: z.string().nullable(),
});

export type StreamSettings = z.infer<typeof StreamSettingsSchema>;

// ---------------------------------------------------------------------------
// PUT /api/v1/streaming/max-sessions
// ---------------------------------------------------------------------------

export const MaxSessionsResponseSchema = z.object({
  max_sessions: z.number().int().positive(),
});

export type MaxSessionsResponse = z.infer<typeof MaxSessionsResponseSchema>;
