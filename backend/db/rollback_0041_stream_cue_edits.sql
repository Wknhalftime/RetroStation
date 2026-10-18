-- Rollback for 0041_stream_cue_edits.sql. Run by hand, with the app stopped, in one
-- transaction:
--   psql -1 -f backend/db/rollback_0041_stream_cue_edits.sql <database>
-- This deletes every manual cue edit; the radio falls back to the autocue for all audio.
DROP TABLE stream_cue_edits;
DELETE FROM schema_migrations WHERE version = '0041_stream_cue_edits';
