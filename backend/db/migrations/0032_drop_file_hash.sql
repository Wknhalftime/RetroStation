-- 0032_drop_file_hash.sql
-- Retires the whole-file SHA-256. PR B (0029) replaced it with audio_hash, and no code has read
-- or written file_hash since; the column stayed only so 0029 could be rolled back. Dropping the
-- column would drop its indexes too; they are named here so the intent is explicit.
DROP INDEX IF EXISTS idx_library_files_unhashed_stat;
DROP INDEX IF EXISTS idx_library_files_unhashed;
DROP INDEX IF EXISTS idx_library_files_file_hash;
ALTER TABLE library_files DROP COLUMN IF EXISTS file_hash;
