-- Rollback for 0037_artist_mb_lookup.sql. Run by hand, with the app stopped, in one transaction:
--   psql -1 -f backend/db/rollback_0037_artist_mb_lookup.sql <database>
-- Every artist the linker linked becomes local again, without an MBID. That includes an artist
-- that release enrichment later confirmed with the same MBID: its stamp still says linked.
-- Names and sort names keep their MusicBrainz spelling; the normalized name, which matching
-- uses, never changed. An artist match made since, whose target is the MBID, still resolves:
-- identity matching uses a target it cannot find in the catalog as the MBID itself. Then the
-- stamp columns go.
UPDATE artists
   SET mbid = NULL,
       origin = 'local',
       disambiguation = NULL,
       needs_enhancement = FALSE,
       enhanced_at = NULL
 WHERE mb_lookup_outcome = 'linked';
ALTER TABLE artists
    DROP CONSTRAINT IF EXISTS artists_mb_lookup_outcome_known,
    DROP CONSTRAINT IF EXISTS artists_mb_lookup_pair,
    DROP COLUMN IF EXISTS mb_lookup_outcome,
    DROP COLUMN IF EXISTS mb_lookup_at;
DELETE FROM schema_migrations WHERE version = '0037_artist_mb_lookup';
