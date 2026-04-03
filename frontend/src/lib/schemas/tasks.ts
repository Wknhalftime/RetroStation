import { z } from "zod";

export const TaskInfoSchema = z.object({
  task_id: z.string(),
  task_type: z.string(),
  status: z.string(),
  progress_data: z.record(z.string(), z.unknown()),
  started_at: z.string(),
  updated_at: z.string(),
  completed_at: z.string().nullable(),
});
export type TaskInfo = z.infer<typeof TaskInfoSchema>;
export const TaskListSchema = z.array(TaskInfoSchema);
export type TaskList = z.infer<typeof TaskListSchema>;
