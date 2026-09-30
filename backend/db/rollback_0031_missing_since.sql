-- Rollback for 0031_missing_since.sql. Run by hand, with the app stopped, in one
-- transaction:  psql -1 -f backend/db/rollback_0031_missing_since.sql <database>
-- (-1 wraps the whole file in a single transaction, so a failure leaves nothing half-done.)
-- Code from before PR C neither reads nor writes missing_since.
DROP INDEX IF EXISTS idx_library_files_missing;
ALTER TABLE library_files DROP COLUMN IF EXISTS missing_since;
DELETE FROM schema_migrations WHERE version = '0031_missing_since';
