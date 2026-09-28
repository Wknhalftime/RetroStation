-- Rollback for 0029_stream_cues.sql
-- Drops the cue cache. Nothing else depends on it; the analyser rebuilds it.

DROP TABLE IF EXISTS stream_cues;

DELETE FROM schema_migrations WHERE version = '0029_stream_cues';
