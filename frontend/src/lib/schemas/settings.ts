import { z } from 'zod'

export const SettingsSchema = z.object({})
export const PathConfigSchema = z.object({})

export type Settings = z.infer<typeof SettingsSchema>
export type PathConfig = z.infer<typeof PathConfigSchema>
