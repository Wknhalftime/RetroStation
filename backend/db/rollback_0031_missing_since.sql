-- Rollback for 0031_missing_since.sql. Run by hand with the app stopped.
-- Code from before PR C neither reads nor writes missing_since.
DROP INDEX IF EXISTS idx_library_files_missing;
ALTER TABLE library_files DROP COLUMN IF EXISTS missing_since;
DELETE FROM schema_migrations WHERE version = '0031_missing_since';
