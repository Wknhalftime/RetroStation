import { z } from "zod";

// Versions phase 2b PR 3 (S5, S7, S11, S12): the version texts of a work's matched lines, and
// the Unmapped versions page.

export const VersionSuggestionSchema = z.object({
  file_id: z.string().uuid(),
  reason: z.enum(["matched_file", "only_copy"]),
  track_title: z.string().nullable(),
  file_path: z.string(),
});

export const WorkVersionSchema = z.object({
  version_text: z.string(),
  version_type: z.string(),
  reads_as_version: z.boolean(),
  state: z.enum(["unmapped", "mapped", "dismissed"]),
  mapped_file_id: z.string().uuid().nullable(),
  plays: z.number(),
  stations: z.array(z.string()),
  first_seen: z.string().nullable(),
  first_station: z.string().nullable(),
  new: z.boolean(),
  suggestion: VersionSuggestionSchema.nullable(),
});

export const MasterFlagSchema = z.object({
  master_file_id: z.string().uuid(),
  version_type: z.string(),
  plain_file_ids: z.array(z.string().uuid()),
});

export const UnmappedVersionSchema = WorkVersionSchema.omit({
  state: true,
  mapped_file_id: true,
}).extend({
  work_id: z.string(),
  work_title: z.string(),
  artist_id: z.string(),
  artist_name: z.string(),
});

export const UnmappedVersionPageSchema = z.object({
  items: z.array(UnmappedVersionSchema),
  total: z.number(),
  new_total: z.number(),
});

export const DismissResultSchema = z.object({ dismissed: z.number() });

export type VersionSuggestion = z.infer<typeof VersionSuggestionSchema>;
export type WorkVersion = z.infer<typeof WorkVersionSchema>;
export type MasterFlag = z.infer<typeof MasterFlagSchema>;
export type UnmappedVersion = z.infer<typeof UnmappedVersionSchema>;
export type UnmappedVersionPage = z.infer<typeof UnmappedVersionPageSchema>;
export type DismissResult = z.infer<typeof DismissResultSchema>;
