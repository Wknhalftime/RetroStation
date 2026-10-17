-- Rollback for 0039_stream_cues_audio_silent.sql. Run by hand, with the app stopped, in one
-- transaction:  psql -1 -f backend/db/rollback_0039_stream_cues_audio_silent.sql <database>
-- Code from before D111 neither reads nor writes audio_silent. The rows 0039 deleted (failed,
-- or at the floor) are not restored: the cue analysis writes them again. A silent row stays a
-- fallback row (analysis_failed = true), which the old code plays whole, as before D111.
ALTER TABLE stream_cues DROP COLUMN audio_silent;
DELETE FROM schema_migrations WHERE version = '0039_stream_cues_audio_silent';
