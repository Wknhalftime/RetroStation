-- Rollback for 0033_stream_cues_orphaned_at.sql. Run by hand, with the app stopped, in one
-- transaction:  psql -1 -f backend/db/rollback_0033_stream_cues_orphaned_at.sql <database>
-- Code from before PR E1 neither reads nor writes orphaned_at.
ALTER TABLE stream_cues DROP COLUMN orphaned_at;
DELETE FROM schema_migrations WHERE version = '0033_stream_cues_orphaned_at';
