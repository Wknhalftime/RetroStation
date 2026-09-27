-- Rollback for 0029_audio_hash.sql. Run by hand with the app stopped.
-- Code from before PR B reads file_hash, which PR B stopped writing: after
-- rolling back, run library_hash_backfill_task so rows indexed meanwhile get
-- a content hash again (move detection falls back to size + mtime until then).
DROP INDEX IF EXISTS idx_library_files_stat;
DROP INDEX IF EXISTS idx_library_files_audio_unhashed;
DROP INDEX IF EXISTS idx_library_files_audio_hash;
ALTER TABLE library_files DROP COLUMN IF EXISTS audio_hash;
DELETE FROM schema_migrations WHERE version = '0029_audio_hash';
