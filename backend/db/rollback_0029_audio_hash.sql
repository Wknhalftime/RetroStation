-- Rollback for 0029_audio_hash.sql. Run by hand with the app stopped.
-- Code from before PR B reads file_hash, which PR B stopped writing: the
-- upsert leaves it untouched, so a row retagged under PR B keeps a stale
-- file_hash and a row added under PR B has none. After rolling back, run
-- library_hash_backfill_task so the NULL rows get a content hash again (move
-- detection falls back to size + mtime until then). The next full scan
-- re-hashes every file; a row whose stale hash no longer matches has its
-- enrichment reset, once.
DROP INDEX IF EXISTS idx_library_files_stat;
DROP INDEX IF EXISTS idx_library_files_audio_unhashed;
DROP INDEX IF EXISTS idx_library_files_audio_hash;
ALTER TABLE library_files DROP COLUMN IF EXISTS audio_hash;
DELETE FROM schema_migrations WHERE version = '0029_audio_hash';
