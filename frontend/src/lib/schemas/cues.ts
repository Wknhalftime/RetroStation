import { z } from "zod";

// ---------------------------------------------------------------------------
// Cue editor wire shapes (backend/routers/library/cues.py)
// ---------------------------------------------------------------------------

export const MarkersSchema = z.object({
  start_ms: z.number(),
  eom_ms: z.number(),
  fade_at_ms: z.number(),
  stop_ms: z.number(),
});

// The four markers in API names; cueMath converts them to and from its own Markers.
export type ApiMarkers = z.infer<typeof MarkersSchema>;

export const CueAnalysisSchema = z.enum(["measured", "failed", "waiting", "silent"]);

export type CueAnalysis = z.infer<typeof CueAnalysisSchema>;

// GET /api/v1/library/files/{file_id}/cues
export const CueEditorSchema = z.object({
  file_id: z.string(),
  title: z.string().nullable(),
  artist: z.string().nullable(),
  format: z.string(),
  duration_ms: z.number(),
  analysis: CueAnalysisSchema,
  automatic: MarkersSchema,
  manual: MarkersSchema.nullable(),
});

export type CueEditor = z.infer<typeof CueEditorSchema>;

// GET /api/v1/library/files/{file_id}/waveform — `peaks` is base64 of one byte per peak
export const WaveformSchema = z.object({
  duration_ms: z.number(),
  per_second: z.number(),
  peaks: z.string(),
});

export type Waveform = z.infer<typeof WaveformSchema>;

export function decodePeaks(b64: string): Uint8Array {
  const binary = atob(b64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes;
}
