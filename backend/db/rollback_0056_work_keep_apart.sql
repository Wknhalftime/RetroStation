-- Rollback of 0056_work_keep_apart (work merges phase 3b PR 5c, SPEC D40): the "Keep apart"
-- marks go, with their capture trigger, and 0056 is forgotten. The merge log's captured rows of
-- the table stay in work_merge_rows; S10's restore skips a table that no longer exists.

DROP TABLE IF EXISTS work_keep_apart;

DELETE FROM schema_migrations WHERE version = '0056_work_keep_apart';
