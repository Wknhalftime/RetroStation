-- Restore the indexes 0039 dropped and the default fillfactor, then forget the migration.
CREATE INDEX IF NOT EXISTS idx_progress_status_time ON progress_tracking(status, updated_at);
CREATE INDEX IF NOT EXISTS idx_progress_stale
    ON progress_tracking(updated_at)
    WHERE status = 'running';
ALTER TABLE progress_tracking RESET (fillfactor);
ALTER TABLE progress_tracking SET WITHOUT CLUSTER;
DELETE FROM schema_migrations WHERE version = '0039_progress_tracking_hot_updates';
