import { z } from "zod";

export const SystemLogEntrySchema = z.object({
  id: z.string(),
  trace_id: z.string().nullable(),
  category: z.string(),
  level: z.string(),
  message: z.string(),
  details: z.record(z.string(), z.unknown()).nullable(),
  created_at: z.string(),
});
export type SystemLogEntry = z.infer<typeof SystemLogEntrySchema>;

export const SystemLogPageSchema = z.object({
  total: z.number(),
  items: z.array(SystemLogEntrySchema),
});
export type SystemLogPage = z.infer<typeof SystemLogPageSchema>;
