-- 0026_deferred_file_hash.sql
-- A first scan into an empty library stores tags and stats first and
-- library_hash_backfill_task fills in content hashes afterwards, so a row
-- may have no hash for a while. NULL means "not fingerprinted yet".
ALTER TABLE library_files ALTER COLUMN file_hash DROP NOT NULL;

-- The backfill's work queue, in the order it reads files.
CREATE INDEX idx_library_files_unhashed
    ON library_files (file_path)
    WHERE file_hash IS NULL AND file_status = 'present';

-- Move detection for rows not hashed yet matches on size + mtime.
CREATE INDEX idx_library_files_unhashed_stat
    ON library_files (file_size, file_mtime_ns)
    WHERE file_hash IS NULL;
