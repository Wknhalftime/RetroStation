-- Rollback for 0054_work_merge_log.sql (work merges, phase 3b PR 3). Run by hand, with the app
-- stopped, in one transaction, after backend/db/rollback_work_merges.sql:
--   psql -1 -f backend/db/rollback_0054_work_merge_log.sql <database>
-- It drops the capture triggers and their function, the merge log, the rollback map and the
-- redirects, and deletes the schema_migrations row.
SET LOCAL lock_timeout = '5s';

DROP TRIGGER IF EXISTS work_merge_capture ON artists;
DROP TRIGGER IF EXISTS work_merge_capture ON artist_names;
DROP TRIGGER IF EXISTS work_merge_capture ON works;
DROP TRIGGER IF EXISTS work_merge_capture ON recordings;
DROP TRIGGER IF EXISTS work_merge_capture ON library_files;
DROP TRIGGER IF EXISTS work_merge_capture ON performances;
DROP TRIGGER IF EXISTS work_merge_capture ON performance_credits;
DROP TRIGGER IF EXISTS work_merge_capture ON song_masters;
DROP TRIGGER IF EXISTS work_merge_capture ON format_overrides;
DROP TRIGGER IF EXISTS work_merge_capture ON version_entry_states;
DROP TRIGGER IF EXISTS work_merge_capture ON matches;
DROP TRIGGER IF EXISTS work_merge_capture ON credit_pick_proposals;
DROP FUNCTION IF EXISTS work_merge_capture();

DROP TABLE IF EXISTS work_redirects;
DROP TABLE IF EXISTS work_merge_rows;
DROP TABLE IF EXISTS work_merges;

DELETE FROM schema_migrations WHERE version = '0054_work_merge_log';
