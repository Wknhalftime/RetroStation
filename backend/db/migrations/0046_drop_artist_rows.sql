-- Artist model phase 1c (SPEC S10, S11; M35, C34, C70; AUD-R035, AUD-R045, AUD-R060 to R070):
-- the artist rows of matches, 1a's and 1b's rollback net, go. Since 1a a log name's links live
-- in log_name_links, and each link 0044 copied keeps its row in its v0 evidence. A match is a
-- song's from here: the artist column goes with its CHECK, UNIQUE, foreign key and index, and
-- identity_id is NOT NULL. 0044's stub list goes too, and C34's two broadcast_artists columns
-- (resolved_artist_mbid exists only on the dev database, so every drop says IF EXISTS). Song
-- rows are not touched. Rollback: backend/db/rollback_0046_drop_artist_rows.sql, before 0045's
-- and 0044's (newest first).
SET LOCAL lock_timeout = '5s';

-- E5: the tables changed here, locked first, ingest's one (broadcast_artists) before matches,
-- which the matcher writes after the names. A timeout fails the deploy loudly.
LOCK TABLE broadcast_artists, matches IN ACCESS EXCLUSIVE MODE;

DELETE FROM matches WHERE artist_id IS NOT NULL;
ALTER TABLE matches DROP CONSTRAINT IF EXISTS xor_match_target;
ALTER TABLE matches DROP CONSTRAINT IF EXISTS matches_artist_id_target_id_key;
DROP INDEX IF EXISTS idx_matches_artist;
ALTER TABLE matches DROP COLUMN IF EXISTS artist_id;  -- and its foreign key
ALTER TABLE matches ALTER COLUMN identity_id SET NOT NULL;

DROP TABLE IF EXISTS link_copy_stubs;

-- S11, C34: never written (artist_candidates) or read by nothing (resolved_artist_mbid).
ALTER TABLE broadcast_artists DROP COLUMN IF EXISTS artist_candidates;
ALTER TABLE broadcast_artists DROP COLUMN IF EXISTS resolved_artist_mbid;
