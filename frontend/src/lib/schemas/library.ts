import { z } from "zod";

export const LibraryStatusSchema = z.object({
  total_files: z.number(),
  quarantine_count: z.number(),
  by_format: z.record(z.string(), z.number()),
  by_enrichment: z.record(z.string(), z.number()),
});

export const LibraryFileSchema = z.object({
  id: z.string().uuid(),
  file_path: z.string(),
  format: z.string(),
  bitrate: z.number().nullable(),
  duration_ms: z.number().nullable(),
  track_title: z.string().nullable(),
  release_title: z.string().nullable(),
  enrichment_status: z.string(),
});

export type LibraryStatus = z.infer<typeof LibraryStatusSchema>;
export type LibraryFile = z.infer<typeof LibraryFileSchema>;
