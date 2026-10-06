-- Rollback for 0035_track_identity_rejected_files.sql. Run by hand with the app stopped, in one
-- transaction:
--   psql -1 -f backend/db/rollback_0035_track_identity_rejected_files.sql <database>
-- Drops every recorded song rejection; the matcher then treats rejected songs like any other.
ALTER TABLE track_identities DROP COLUMN IF EXISTS rejected_file_ids;
DELETE FROM schema_migrations WHERE version = '0035_track_identity_rejected_files';
