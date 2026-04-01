import { z } from 'zod'

export const StationSchema = z.object({})
export const StationListSchema = z.array(StationSchema)
export const StationDashboardSchema = z.object({})

export type Station = z.infer<typeof StationSchema>
export type StationDashboard = z.infer<typeof StationDashboardSchema>
