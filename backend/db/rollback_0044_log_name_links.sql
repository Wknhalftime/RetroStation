-- Rollback for 0044_log_name_links.sql. Run by hand, with the app stopped, in one transaction:
--   psql -1 -f backend/db/rollback_0044_log_name_links.sql <database>
-- The links go; the artist rows of matches, which 0044 copied and phase 1a never wrote, are
-- the links again for the code before 1a (M35). The stubs 0044 made go too, unless a work
-- points at one now. Stubs made since by the matcher or the Resolve route stay: they are
-- ordinary artists with an MBID.
DROP TABLE IF EXISTS log_name_links;
DELETE FROM artists a
 USING link_copy_stubs s
 WHERE a.id = s.artist_id
   AND NOT EXISTS (SELECT 1 FROM works w WHERE w.artist_id = a.id);
DROP TABLE IF EXISTS link_copy_stubs;
DELETE FROM schema_migrations WHERE version = '0044_log_name_links';
