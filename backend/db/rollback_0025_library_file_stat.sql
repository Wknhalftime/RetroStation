-- Rollback for 0025_library_file_stat.sql
-- Drops the stat columns. folder_hash baselines are not restored: the
-- watcher recomputes them on its next poll, which (under the old Merkle
-- scheme this rolls back to) re-detects every folder once.

ALTER TABLE library_files DROP COLUMN IF EXISTS file_mtime_ns;
ALTER TABLE library_files DROP COLUMN IF EXISTS file_size;

DELETE FROM schema_migrations WHERE version = '0025_library_file_stat';
