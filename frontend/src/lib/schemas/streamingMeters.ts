import { z } from "zod";

// ---------------------------------------------------------------------------
// GET /api/v1/streaming/cue-coverage (G2, D77/PG7)
// ---------------------------------------------------------------------------

export const CueCoverageSchema = z.object({
  analysable: z.number().int().nonnegative(),
  ready: z.number().int().nonnegative(),
  failed: z.number().int().nonnegative(),
  unhashed: z.number().int().nonnegative(),
});

export type CueCoverage = z.infer<typeof CueCoverageSchema>;

// ---------------------------------------------------------------------------
// The audio-engine cost meter row (G2, D90/D91/D95, design note 15). This is
// never a route response: it travels as the progress_data of the one
// "stream_resources" progress row, read from the progress store over /ws.
// ---------------------------------------------------------------------------

export const CostSourceSchema = z.enum(["measured", "reference"]);
export type CostSource = z.infer<typeof CostSourceSchema>;

export const LimitedBySchema = z.enum(["processor", "memory"]);
export type LimitedBy = z.infer<typeof LimitedBySchema>;

export const EngineReadingSchema = z.object({
  cpu_percent: z.number().nonnegative().nullable(),
  memory_mb: z.number().nonnegative(),
  warming: z.boolean(),
});
export type EngineReading = z.infer<typeof EngineReadingSchema>;

export const SessionCostSchema = z.object({
  cpu_percent: z.number().nonnegative(),
  memory_mb: z.number().nonnegative(),
  source: CostSourceSchema,
});
export type SessionCost = z.infer<typeof SessionCostSchema>;

export const MachineTotalsSchema = z.object({
  threads: z.number().int().positive(),
  memory_total_mb: z.number().nonnegative(),
  memory_available_mb: z.number().nonnegative(),
  cpu_percent: z.number().nonnegative().nullable(),
});
export type MachineTotals = z.infer<typeof MachineTotalsSchema>;

export const CapacityBudgetSchema = z.object({
  cpu_share: z.number().positive(),
  memory_share: z.number().positive(),
});
export type CapacityBudget = z.infer<typeof CapacityBudgetSchema>;

export const ResourceMeterSchema = z.object({
  open_streams: z.number().int().nonnegative(),
  scope: z.literal("audio_engines"),
  streams: z.array(EngineReadingSchema),
  cost: SessionCostSchema,
  machine: MachineTotalsSchema,
  suggested_max: z.number().int().positive(),
  limited_by: LimitedBySchema,
  budget: CapacityBudgetSchema,
});

export type ResourceMeter = z.infer<typeof ResourceMeterSchema>;
