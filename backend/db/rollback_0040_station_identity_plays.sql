-- Rollback for 0040_station_identity_plays.sql. Run by hand, with the app stopped, in one
-- transaction:
--   psql -1 -f backend/db/rollback_0040_station_identity_plays.sql <database>
-- The missing-matches report then needs its play_events count back (before this migration).
DROP TRIGGER IF EXISTS playlists_identity_plays_move ON playlists;
DROP TRIGGER IF EXISTS playlists_identity_plays_delete ON playlists;
DROP TRIGGER IF EXISTS play_events_identity_plays_truncate ON play_events;
DROP TRIGGER IF EXISTS play_events_identity_plays_update ON play_events;
DROP TRIGGER IF EXISTS play_events_identity_plays_delete ON play_events;
DROP TRIGGER IF EXISTS play_events_identity_plays_insert ON play_events;
DROP FUNCTION IF EXISTS play_events_identity_plays_truncate();
DROP FUNCTION IF EXISTS playlists_identity_plays_move();
DROP FUNCTION IF EXISTS playlists_identity_plays_delete();
DROP FUNCTION IF EXISTS play_events_identity_plays_sync();
DROP FUNCTION IF EXISTS station_identity_plays_apply(station_identity_plays[]);
DROP TABLE IF EXISTS station_identity_plays;
DELETE FROM schema_migrations WHERE version = '0040_station_identity_plays';
