-- MusicBrainz cache fill, phase 4.1 PR 2 (SPEC S3; M7; R3; AUD-R042, AUD-R091).
-- The confirmations queue: (log name, title) pairs a later step queues for the fill to search,
-- such as 4.4's add-only pass. One row per name and normalized title; the fill leaves the rows in
-- place (their life cycle is 4.4's). The key to broadcast_artists cascades, so deleting a log name
-- deletes its queued pairs (R3); UNIQUE's index leads with that key, so the cascade needs no other.
-- A new, empty table: no rows copied, so no ANALYZE. No rollback script: from 2a on, the
-- rollback is the dump (2a's D4).
SET LOCAL lock_timeout = '5s';

CREATE TABLE mb_cache_fill_confirmations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    broadcast_artist_id uuid NOT NULL REFERENCES broadcast_artists (id) ON DELETE CASCADE,
    normalized_title text NOT NULL,
    title text NOT NULL,
    queued_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (broadcast_artist_id, normalized_title)
);
