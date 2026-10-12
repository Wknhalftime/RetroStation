-- 0038 only deleted cache rows; the cache refills itself, so there is nothing to restore.
-- Forget the migration so it can be applied again.
DELETE FROM schema_migrations WHERE version = '0038_purge_slim_recording_search_cache';
