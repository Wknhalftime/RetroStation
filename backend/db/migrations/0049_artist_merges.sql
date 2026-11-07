-- Artist model phase 2a, PR 3 (SPEC S11; M33; AUD-R041): Merge and Unmerge on the artist page.
-- artist_merges holds each direct edge: the artist merged away (artist_id, one edge each) and its
-- primary. A chain is kept as made; the group's primary is found by walking to the root. Merging is
-- only ever the curator's action, so merged_by is the constant 'curator'. An artist on either side
-- of an edge is never deleted by _delete_unused_local_artists, so the foreign keys don't cascade.
--
-- No rollback script (D4): the rollback is the dump (phase 7 S5).
SET LOCAL lock_timeout = '5s';

CREATE TABLE artist_merges (
    artist_id  TEXT PRIMARY KEY REFERENCES artists(id),
    primary_id TEXT NOT NULL REFERENCES artists(id),
    merged_by  TEXT NOT NULL DEFAULT 'curator' CHECK (merged_by = 'curator'),
    merged_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    reason     TEXT,
    CHECK (artist_id <> primary_id)
);
CREATE INDEX idx_artist_merges_primary ON artist_merges (primary_id);
