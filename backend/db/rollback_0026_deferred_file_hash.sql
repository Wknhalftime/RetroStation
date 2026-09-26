-- Rollback for 0026_deferred_file_hash.sql
-- Precondition: the hash backfill must have finished (no library_files row
-- with file_hash IS NULL) before this runs. SET NOT NULL below fails
-- otherwise, since a still-unhashed row would violate the constraint.

DROP INDEX IF EXISTS idx_library_files_unhashed;
DROP INDEX IF EXISTS idx_library_files_unhashed_stat;

ALTER TABLE library_files ALTER COLUMN file_hash SET NOT NULL;

DELETE FROM schema_migrations WHERE version = '0026_deferred_file_hash';
