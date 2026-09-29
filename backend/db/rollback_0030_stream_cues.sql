-- Rollback for 0030_stream_cues.sql. Run by hand with the app stopped.
-- Drops the two views, the play_date expression index and the cue table, in
-- dependency order, and forgets the migration. The tune-in schedule reader
-- (PgPlayableScheduleRepository) requires both views and stream_cues, so roll
-- the code back with this script. The M3U export does not use the views yet.
-- The analyser rebuilds stream_cues once the migration is reapplied.
DROP VIEW IF EXISTS station_day_plays;
DROP VIEW IF EXISTS play_file_resolution;
DROP INDEX IF EXISTS idx_play_events_play_date;
DROP TABLE IF EXISTS stream_cues;
DELETE FROM schema_migrations WHERE version = '0030_stream_cues';
