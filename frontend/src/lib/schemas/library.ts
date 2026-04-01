import { z } from 'zod'

export const LibraryStatusSchema = z.object({})
export const LibraryFileSchema = z.object({})

export type LibraryStatus = z.infer<typeof LibraryStatusSchema>
export type LibraryFile = z.infer<typeof LibraryFileSchema>
