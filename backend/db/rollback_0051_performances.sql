-- Rollback for 0051_performances.sql (performances, phase 3a PR 2). Run by hand, with the app
-- stopped, in one transaction:
--   psql -1 -f backend/db/rollback_0051_performances.sql <database>
-- It drops matches.performance_id, library_files.performance_id with its key, CHECK and
-- index, the two tables, and the schema_migrations row. Files, works and matches stay.
SET LOCAL lock_timeout = '5s';

ALTER TABLE matches DROP COLUMN performance_id;
ALTER TABLE performances DROP CONSTRAINT performances_preferred_copy_fkey;
ALTER TABLE library_files DROP CONSTRAINT library_files_performance_work_fkey;
ALTER TABLE library_files DROP CONSTRAINT library_files_performance_needs_work;
ALTER TABLE library_files DROP CONSTRAINT library_files_id_performance_id_key;
DROP INDEX idx_library_files_performance_work;
ALTER TABLE library_files DROP COLUMN performance_id;
DROP TABLE performance_credits;
DROP TABLE performances;

DELETE FROM schema_migrations WHERE version = '0051_performances';
