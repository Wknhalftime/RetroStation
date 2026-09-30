-- Rollback for 0032_drop_file_hash.sql. Run by hand with the app stopped, in one transaction:
--   psql -1 -f backend/db/rollback_0032_drop_file_hash.sql <database>
-- It restores the column and its indexes but not the values: every row comes back with a NULL
-- file_hash. Rolling back further (0029, 0026) needs this rollback first; code from before PR B
-- then re-hashes the NULL rows through library_hash_backfill_task.
ALTER TABLE library_files ADD COLUMN IF NOT EXISTS file_hash TEXT;

CREATE INDEX IF NOT EXISTS idx_library_files_file_hash ON library_files (file_hash);

CREATE INDEX IF NOT EXISTS idx_library_files_unhashed
    ON library_files (file_path)
    WHERE file_hash IS NULL AND file_status = 'present';

CREATE INDEX IF NOT EXISTS idx_library_files_unhashed_stat
    ON library_files (file_size, file_mtime_ns)
    WHERE file_hash IS NULL;

DELETE FROM schema_migrations WHERE version = '0032_drop_file_hash';
