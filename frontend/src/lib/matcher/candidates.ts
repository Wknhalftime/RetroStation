/**
 * Helpers for extracting structured data out of `QueueArtist.candidates`.
 *
 * The backend serializes candidates as `list[dict] | None` of arbitrary shape
 * (it mirrors the MusicBrainz search response payload), so the frontend
 * receives `Array<Record<string, unknown> | null> | null` via Zod's
 * `z.array(z.record(z.unknown()).nullable()).nullable()` schema. This module
 * centralizes the narrowing so call sites don't each hand-roll casts.
 */
import type { QueueArtist } from '@/lib/schemas/matcher'

/**
 * Return the first candidate's MusicBrainz artist ID if present, otherwise
 * null. Safe against: `candidates` being null, empty, first entry being null,
 * or `mbid` not being a string on the first entry.
 */
export function firstCandidateMbid(artist: QueueArtist | null): string | null {
  if (!artist) return null
  const first = artist.candidates?.[0]
  if (!first || typeof first !== 'object') return null
  const mbid = (first as Record<string, unknown>)['mbid']
  return typeof mbid === 'string' ? mbid : null
}
