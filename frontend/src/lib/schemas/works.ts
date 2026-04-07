import { z } from "zod";

// ---------------------------------------------------------------------------
// File info
// ---------------------------------------------------------------------------

export const FileInfoSchema = z.object({
  id: z.string().uuid(),
  file_path: z.string(),
  format: z.string(),
  bitrate: z.number().nullable(),
  duration_ms: z.number().nullable(),
  track_title: z.string().nullable(),
  release_title: z.string().nullable(),
  enrichment_status: z.string(),
});

export type FileInfo = z.infer<typeof FileInfoSchema>;

// ---------------------------------------------------------------------------
// Recording detail
// ---------------------------------------------------------------------------

export const RecordingDetailSchema = z.object({
  id: z.string(),
  title: z.string(),
  version_type: z.string(),
  duration_ms: z.number().nullable(),
  files: z.array(FileInfoSchema),
});

export type RecordingDetail = z.infer<typeof RecordingDetailSchema>;

// ---------------------------------------------------------------------------
// Song master info
// ---------------------------------------------------------------------------

export const SongMasterInfoSchema = z.object({
  id: z.string().uuid(),
  preferred_file_id: z.string().uuid(),
  selection_method: z.string(),
  score: z.number().nullable(),
  updated_at: z.string(),
});

export type SongMasterInfo = z.infer<typeof SongMasterInfoSchema>;

// ---------------------------------------------------------------------------
// Format override info
// ---------------------------------------------------------------------------

export const FormatOverrideInfoSchema = z.object({
  id: z.string().uuid(),
  format_name: z.string(),
  preferred_file_id: z.string().uuid(),
  notes: z.string().nullable(),
  created_at: z.string(),
});

export type FormatOverrideInfo = z.infer<typeof FormatOverrideInfoSchema>;

// ---------------------------------------------------------------------------
// Work detail (top-level)
// ---------------------------------------------------------------------------

export const WorkDetailSchema = z.object({
  id: z.string(),
  title: z.string(),
  artist_id: z.string(),
  recordings: z.array(RecordingDetailSchema),
  song_master: SongMasterInfoSchema.nullable(),
  format_overrides: z.array(FormatOverrideInfoSchema),
  mbid: z.string().nullable().default(null),
  origin: z.enum(["local", "musicbrainz"]).default("local"),
});

export type WorkDetail = z.infer<typeof WorkDetailSchema>;
