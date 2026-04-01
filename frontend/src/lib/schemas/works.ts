import { z } from 'zod'

export const WorkSchema = z.object({})
export const WorkFilesTableRowSchema = z.object({})

export type Work = z.infer<typeof WorkSchema>
export type WorkFilesTableRow = z.infer<typeof WorkFilesTableRowSchema>
