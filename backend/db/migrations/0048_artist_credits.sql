-- Artist model phase 2a, PR 2 (SPEC S1, S10; C26, C43; AUD-R032, AUD-R041): every artist of a file
-- and every name of an artist. library_files.artist_mbids holds each MBID of the artist-MBID tag
-- (NULL until filled, '{}' when untagged). credit holds them in order with their names and joining
-- words, {name, mbid, join}, either null until known. The name columns and the scalar artist_mbid
-- stay as they are (C26). backfill_artist_credits_task fills existing rows.
--
-- artist_names holds MusicBrainz names, cached aliases and tag spellings. There is no source for a
-- log spelling, so one is refused. A name goes with its artist, because
-- _delete_unused_local_artists deletes local artists.
--
-- No rollback script (D4): the rollback is the dump (phase 7 S5).
SET LOCAL lock_timeout = '5s';

ALTER TABLE library_files ADD COLUMN artist_mbids TEXT[], ADD COLUMN credit JSONB;
CREATE INDEX idx_library_files_artist_mbids ON library_files USING GIN (artist_mbids);

CREATE TABLE artist_names (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    artist_id       TEXT NOT NULL REFERENCES artists(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    source          TEXT NOT NULL
                    CHECK (source IN ('musicbrainz_name', 'musicbrainz_alias', 'tag')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (artist_id, name, source)
);
CREATE INDEX idx_artist_names_normalized_name ON artist_names (normalized_name);
