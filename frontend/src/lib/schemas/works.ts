import { z } from "zod";
import { MasterFlagSchema, WorkVersionSchema } from "@/lib/schemas/versions";

// ---------------------------------------------------------------------------
// File info
// ---------------------------------------------------------------------------

// The work a file is on, when a work's page shows it through a shared recording.
export const OtherWorkRefSchema = z.object({
  id: z.string(),
  title: z.string(),
  artist_name: z.string(),
  // 3b PR 5a (D41 h): the work's credited artists.
  artists: z.array(z.string()).optional(),
});

export type OtherWorkRef = z.infer<typeof OtherWorkRefSchema>;

export const FileInfoSchema = z.object({
  id: z.string().uuid(),
  file_path: z.string(),
  format: z.string(),
  bitrate: z.number().nullable(),
  duration_ms: z.number().nullable(),
  track_title: z.string().nullable(),
  release_title: z.string().nullable(),
  enrichment_status: z.string(),
  file_status: z.enum(["present", "missing", "deleted"]),
  other_work: OtherWorkRefSchema.nullish(),
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
  // Whole points from master selection (release status + release type + format,
  // 0-190; see backend/services/master_selection_service.py), not a fraction.
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

// ---------------------------------------------------------------------------
// Performances, their copies and the lines pinned on them (3b PR 5a; S8, D41)
// ---------------------------------------------------------------------------

export const CreditedArtistSchema = z.object({
  id: z.string().nullable(),
  name: z.string(),
});

export type CreditedArtist = z.infer<typeof CreditedArtistSchema>;

export const PinnedLineSchema = z.object({
  identity_id: z.string(),
  station_id: z.string(),
  station: z.string(),
  title: z.string(),
  artist: z.string(),
  plays: z.number(),
  next_file_id: z.string().nullable(),
  next_path: z.string().nullable(),
});

export type PinnedLine = z.infer<typeof PinnedLineSchema>;

export const CopySchema = z.object({
  file: FileInfoSchema,
  pins: z.array(PinnedLineSchema),
});

export type Copy = z.infer<typeof CopySchema>;

export const PerformanceSchema = z.object({
  id: z.string(),
  credit: z.array(CreditedArtistSchema),
  credit_text: z.string(),
  version_type: z.string(),
  default: z.boolean(),
  preferred_copy_id: z.string().nullable(),
  lines: z.number(),
  copies: z.array(CopySchema),
});

export type Performance = z.infer<typeof PerformanceSchema>;

export const ActSchema = z.object({
  credit: z.string(),
  default: z.boolean(),
  lines: z.number(),
  reached: z.record(z.string(), z.number()),
});

export type Act = z.infer<typeof ActSchema>;

export const WorkDetailSchema = z.object({
  id: z.string(),
  title: z.string(),
  artist_id: z.string(),
  recordings: z.array(RecordingDetailSchema),
  song_master: SongMasterInfoSchema.nullable(),
  format_overrides: z.array(FormatOverrideInfoSchema),
  // Phase 2b S5, S12: optional, so a work detail without them still shows the page.
  versions: z.array(WorkVersionSchema).optional(),
  master_flag: MasterFlagSchema.nullable().optional(),
  mbid: z.string().nullable().default(null),
  origin: z.enum(["local", "musicbrainz"]).default("local"),
  // 3b PR 5a (S8, D41 a, f, g): optional, so a detail without them still shows the page.
  artists: z.array(CreditedArtistSchema).optional(),
  performances: z.array(PerformanceSchema).optional(),
  acts: z.array(ActSchema).optional(),
});

export type WorkDetail = z.infer<typeof WorkDetailSchema>;
