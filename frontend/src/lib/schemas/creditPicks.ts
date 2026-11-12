import { z } from "zod";

// Performances phase 3a PR 3 (S9): the Credit picks page, its count on Library Status, Apply and
// a version line's pick.

export const CreditPickFileSchema = z.object({
  file_id: z.string(),
  file_path: z.string(),
  track_title: z.string().nullable(),
  file_status: z.string(),
  source: z.string().nullable().optional(),
});

export const CreditPickCandidateSchema = z.object({
  performance_id: z.string(),
  credit_names: z.array(z.string()),
  would_play: CreditPickFileSchema.nullable(),
});

export const CreditPickSchema = z.object({
  id: z.string(),
  identity_id: z.string(),
  kind: z.enum(["credit", "version_line", "tie"]),
  reason: z.enum(["credit", "shared_artist"]),
  state: z.enum(["proposed", "skipped", "applied", "picked"]),
  title: z.string(),
  artist_name: z.string(),
  plays: z.number(),
  work_id: z.string(),
  work_title: z.string(),
  performance_id: z.string().nullable(),
  plays_now: CreditPickFileSchema.nullable(),
  would_play: CreditPickFileSchema.nullable(),
  candidates: z.array(CreditPickCandidateSchema),
});

export const CreditPickListSchema = z.object({
  credit: z.array(CreditPickSchema),
  version_lines: z.array(CreditPickSchema),
  total: z.number(),
});

export const CreditPickCountSchema = z.object({ total: z.number() });

export const ApplyCreditPicksResultSchema = z.object({
  applied: z.number(),
  skipped: z.number(),
  stale: z.array(z.string()),
});

export const VersionLinePickResultSchema = z.object({ performance_id: z.string().nullable() });

export type CreditPickFile = z.infer<typeof CreditPickFileSchema>;
export type CreditPickCandidate = z.infer<typeof CreditPickCandidateSchema>;
export type CreditPick = z.infer<typeof CreditPickSchema>;
export type CreditPickList = z.infer<typeof CreditPickListSchema>;
export type CreditPickCount = z.infer<typeof CreditPickCountSchema>;
export type ApplyCreditPicksResult = z.infer<typeof ApplyCreditPicksResultSchema>;
export type VersionLinePickResult = z.infer<typeof VersionLinePickResultSchema>;
