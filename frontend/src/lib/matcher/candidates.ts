/**
 * Helpers for `QueueArtist.candidates`: the name's suggested links, best first (phase 1c S8).
 */
import type { QueueArtist } from "@/lib/schemas/matcher";

/**
 * The first candidate's MusicBrainz artist ID, or null: no artist, no candidate, or a first
 * candidate that is a local artist with no MBID.
 */
export function firstCandidateMbid(artist: QueueArtist | null): string | null {
  return artist?.candidates[0]?.mbid ?? null;
}
