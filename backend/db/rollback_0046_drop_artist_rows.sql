-- Rollback for 0046_drop_artist_rows.sql (artist model phase 1c, SPEC S10). Run by hand, with
-- the app stopped, in one transaction, first of phase 1c's rollbacks (newest first: this one,
-- then 0045's, then 0044's, which reads the artist column and link_copy_stubs):
--   psql -1 -f backend/db/rollback_0046_drop_artist_rows.sql <database>
-- It puts back the artist column of matches with its CHECK, UNIQUE, foreign key and index,
-- makes identity_id nullable again, and recreates link_copy_stubs and
-- broadcast_artists.artist_candidates empty (nothing ever wrote that column;
-- resolved_artist_mbid, dev only and read by nothing, stays gone). An empty link_copy_stubs
-- means 0044's rollback keeps 0044's stubs: they are ordinary artists with an MBID.
-- Then one artist row per v0 copy of one (evidence.legacy), as 0044 found it: each link's, in
-- any state, and each link 0045 deleted, whose row link_demotion_backup keeps (so the backup
-- must stay while this rollback may run). A link written since 1a has no copy and gets no row;
-- a duplicate 0044 collapsed into one link is gone for good.
ALTER TABLE matches ALTER COLUMN identity_id DROP NOT NULL;
ALTER TABLE matches ADD COLUMN artist_id UUID REFERENCES broadcast_artists (id);
ALTER TABLE matches ADD CONSTRAINT xor_match_target CHECK (
    (identity_id IS NOT NULL AND artist_id IS NULL)
    OR (identity_id IS NULL AND artist_id IS NOT NULL)
);
ALTER TABLE matches ADD CONSTRAINT matches_artist_id_target_id_key UNIQUE (artist_id, target_id);
CREATE INDEX idx_matches_artist ON matches (artist_id);

CREATE TABLE link_copy_stubs (
    artist_id TEXT PRIMARY KEY REFERENCES artists (id) ON DELETE CASCADE
);
ALTER TABLE broadcast_artists ADD COLUMN artist_candidates JSONB;

WITH copies AS (
    SELECT l.broadcast_artist_id AS name_id, l.evidence -> 'legacy' AS legacy
      FROM log_name_links l
     WHERE l.evidence ? 'legacy'
    UNION ALL
    SELECT (b.before ->> 'broadcast_artist_id')::uuid, b.before -> 'evidence' -> 'legacy'
      FROM link_demotion_backup b
     WHERE b.kind = 'link' AND b.after IS NULL AND b.before -> 'evidence' ? 'legacy'
)
INSERT INTO matches (id, artist_id, target_id, target_type, confidence_score, match_tier,
                     created_at)
SELECT (c.legacy ->> 'match_id')::uuid,
       c.name_id,
       c.legacy ->> 'target_id',
       'artist',
       (c.legacy ->> 'score')::real,
       c.legacy ->> 'tier',
       (c.legacy ->> 'created_at')::timestamptz
  FROM copies c
 WHERE EXISTS (SELECT 1 FROM broadcast_artists ba WHERE ba.id = c.name_id)
ON CONFLICT DO NOTHING;

DELETE FROM schema_migrations WHERE version = '0046_drop_artist_rows';
