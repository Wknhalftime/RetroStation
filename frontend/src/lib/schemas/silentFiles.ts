import { z } from "zod";

export const SilenceReasonSchema = z.enum(["silent", "low_bitrate", "short_cued_span"]);

export const SilentFileSchema = z.object({
  file_id: z.string().uuid(),
  artist: z.string().nullable(),
  title: z.string().nullable(),
  album: z.string().nullable(),
  path: z.string(),
  duration_ms: z.number().int().nullable(),
  reasons: z.array(SilenceReasonSchema).min(1),
  bitrate_kbps: z.number().int().nonnegative().nullable(),
  cued_span_ms: z.number().int().nonnegative().nullable(),
});

export const SilentFileReportSchema = z.object({
  files: z.array(SilentFileSchema),
  silent: z.number().int().nonnegative(),
  nearly_silent: z.number().int().nonnegative(),
});

export type SilenceReason = z.infer<typeof SilenceReasonSchema>;
export type SilentFile = z.infer<typeof SilentFileSchema>;
export type SilentFileReport = z.infer<typeof SilentFileReportSchema>;
