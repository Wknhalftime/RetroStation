import { z } from "zod";

// Work merges phase 3b PR 4 (S2, D6, D15, D26-D28): the merge list, its count on Library Status
// and Apply. PR 5c (D40, D55-D58): the kept pairs; each new field defaults, so an answer
// without them (an older server, the locked fixtures) still parses.

export const MergeChangeSchema = z.object({
  station_id: z.string(),
  station: z.string(),
  identity_id: z.string(),
  title: z.string(),
  artist: z.string(),
  plays: z.number(),
  from_file_id: z.string().nullable(),
  from_path: z.string().nullable(),
  to_file_id: z.string().nullable(),
  to_path: z.string().nullable(),
});

export const MergeWorkSchema = z.object({
  work_id: z.string(),
  title: z.string(),
  artists: z.array(z.string()),
  plays: z.number(),
  musicbrainz: z.boolean(),
  master: z
    .object({ file_id: z.string(), file_path: z.string().nullable(), method: z.string() })
    .nullable(),
});

export const MergeFlagSchema = z.object({
  kind: z.enum(["masters_meet", "cue_edit", "rejection"]),
  work_id: z.string().nullable(),
  file_id: z.string().nullable(),
  identity_id: z.string().nullable(),
  text: z.string(),
});

export const WorkPairSchema = z.object({
  first_work_id: z.string(),
  second_work_id: z.string(),
});

export const KeptApartSchema = z.object({
  first: z.object({ work_id: z.string(), title: z.string(), artists: z.array(z.string()) }),
  second: z.object({ work_id: z.string(), title: z.string(), artists: z.array(z.string()) }),
  kept_at: z.string(),
  note: z.string(),
  musicbrainz_links: z.boolean(),
});

export const MergeGroupSchema = z.object({
  target_id: z.string(),
  source_ids: z.array(z.string()),
  joins: z.array(z.enum(["work_link", "shared_artist", "shared_name"])),
  plays: z.number(),
  works: z.array(MergeWorkSchema),
  flags: z.array(MergeFlagSchema),
  changes: z.array(MergeChangeSchema),
  failed: z.object({ error: z.string(), logged_at: z.string().nullable() }).nullable(),
  // D56: the kept pairs that hold the group; Apply never sends a held group.
  held: z.array(WorkPairSchema).default([]),
});

export const MergeListSchema = z.object({
  groups: z.array(MergeGroupSchema),
  duplicates: z.array(
    z.object({
      recording_mbid: z.string(),
      works: z.array(z.object({ work_id: z.string(), title: z.string() })),
    })
  ),
  kept_works: z.array(
    z.object({ work_id: z.string(), title: z.string(), references: z.array(z.string()) })
  ),
  kept_apart: z.array(KeptApartSchema).default([]),
  total: z.number(),
  offset: z.number(),
  limit: z.number(),
});

export const MergeListCountSchema = z.object({
  total: z.number(),
  duplicates: z.number(),
  kept_works: z.number(),
  kept_apart: z.number().default(0),
});

export const ApplyMergeListResultSchema = z.object({
  results: z.array(
    z.object({
      target_id: z.string(),
      source_ids: z.array(z.string()),
      outcome: z.enum(["merged", "waiting", "failed", "already_merged", "changed"]),
      changes: z.array(MergeChangeSchema),
      error: z.string().nullable(),
    })
  ),
  merged: z.number(),
  waiting: z.number(),
  failed: z.number(),
  already_merged: z.number(),
  changed: z.number(),
});

export type WorkPair = z.infer<typeof WorkPairSchema>;
export type KeptApart = z.infer<typeof KeptApartSchema>;
export type MergeChange = z.infer<typeof MergeChangeSchema>;
export type MergeWork = z.infer<typeof MergeWorkSchema>;
export type MergeGroup = z.infer<typeof MergeGroupSchema>;
export type MergeList = z.infer<typeof MergeListSchema>;
export type MergeListCount = z.infer<typeof MergeListCountSchema>;
export type ApplyMergeListResult = z.infer<typeof ApplyMergeListResultSchema>;
