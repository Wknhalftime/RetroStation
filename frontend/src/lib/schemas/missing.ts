import { z } from "zod";

export const MissingFileCandidateSchema = z.object({
  id: z.string().uuid(),
  file_path: z.string(),
});

export const MissingFileSchema = z.object({
  id: z.string().uuid(),
  file_path: z.string(),
  artist_name: z.string().nullable(),
  track_title: z.string().nullable(),
  release_title: z.string().nullable(),
  missing_since: z.string().nullable(),
  work_id: z.string().nullable(),
  work_title: z.string().nullable(),
  match_count: z.number().int().nonnegative(),
  work_has_present_file: z.boolean(),
  candidates: z.array(MissingFileCandidateSchema),
});

export const MissingFilePageSchema = z.object({
  items: z.array(MissingFileSchema),
  total: z.number().int().nonnegative(),
  total_match_count: z.number().int().nonnegative(),
});

export const MissingFileDeletionSchema = z.object({
  deleted: z.number().int().nonnegative(),
  matches_released: z.number().int().nonnegative(),
  skipped: z.number().int().nonnegative(),
});

export type MissingFileCandidate = z.infer<typeof MissingFileCandidateSchema>;
export type MissingFile = z.infer<typeof MissingFileSchema>;
export type MissingFilePage = z.infer<typeof MissingFilePageSchema>;
export type MissingFileDeletion = z.infer<typeof MissingFileDeletionSchema>;
