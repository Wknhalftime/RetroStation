-- Rollback for 0044_log_name_links.sql. Run by hand, with the app stopped, in one transaction:
--   psql -1 -f backend/db/rollback_0044_log_name_links.sql <database>
-- The artist rows of matches, which 0044 copied and phase 1a never writes, are the links again
-- for the code before 1a (M35), brought up to date first:
-- - a name an Unmatch put back in review since 0044 loses its rows, as Unmatch deleted them
--   before 1a (the old matcher's plain INSERT would hit UNIQUE (artist_id, target_id));
-- - each active link with no row (written by the matcher or Resolve since 0044) becomes one:
--   its target the artist's MBID, else its id; a manual link tier manual, score 1.0; an
--   automatic one the tier and score of its evidence. Suggested and rejected links are lost.
-- Then the links go, and the stubs 0044 made, unless a work points at one now. Stubs made
-- since by the matcher or the Resolve route stay: they are ordinary artists with an MBID.
DELETE FROM matches m
 USING broadcast_artists ba
 WHERE m.artist_id = ba.id
   AND ba.match_status IN ('pending', 'needs_review');
INSERT INTO matches (artist_id, target_id, target_type, confidence_score, match_tier, created_at)
SELECT l.broadcast_artist_id,
       COALESCE(a.mbid, a.id),
       'artist',
       CASE WHEN l.origin = 'manual' THEN 1.0
            ELSE COALESCE((l.evidence ->> 'score')::real, l.strength) END,
       CASE WHEN l.origin = 'manual' THEN 'manual'
            ELSE COALESCE(l.evidence ->> 'tier', 'unclassified') END,
       l.created_at
  FROM log_name_links l
  JOIN artists a ON a.id = l.artist_id
 WHERE l.state = 'active'
   AND NOT EXISTS (
       SELECT 1 FROM matches m
        WHERE m.artist_id = l.broadcast_artist_id
          AND (m.target_id = a.id OR m.target_id = a.mbid)
   )
ON CONFLICT (artist_id, target_id) DO NOTHING;
DROP TABLE IF EXISTS log_name_links;
DELETE FROM artists a
 USING link_copy_stubs s
 WHERE a.id = s.artist_id
   AND NOT EXISTS (SELECT 1 FROM works w WHERE w.artist_id = a.id);
DROP TABLE IF EXISTS link_copy_stubs;
DELETE FROM schema_migrations WHERE version = '0044_log_name_links';
