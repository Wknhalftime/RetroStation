import { z } from 'zod'

export const ActiveTaskSchema = z.object({})
export const ProgressDataSchema = z.object({})

export type ActiveTask = z.infer<typeof ActiveTaskSchema>
export type ProgressData = z.infer<typeof ProgressDataSchema>
