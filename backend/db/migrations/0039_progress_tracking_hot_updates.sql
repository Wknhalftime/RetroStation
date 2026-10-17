-- progress_tracking is rewritten every few seconds (heartbeats, the cost meter) and read by the
-- /ws feed every 0.5 s. Two indexes held updated_at, so almost no heartbeat could be a HOT
-- update: of 396k updates, 1k were HOT, and the table grew to 11 MB for 412 kB of live rows,
-- which the feed's sequential scan read in full twice a second. Drop them: every reader either
-- reads the whole (small) table or goes through the primary key or idx_progress_type_status,
-- neither of which a heartbeat changes. Half-full pages leave room for a row's new version.
DROP INDEX IF EXISTS idx_progress_status_time;
DROP INDEX IF EXISTS idx_progress_stale;
ALTER TABLE progress_tracking SET (fillfactor = 50);
-- Rewrite now so the bloat goes and the new fillfactor applies to existing rows. CLUSTER on a
-- named table may run inside the runner's transaction; the table holds a few thousand rows.
CLUSTER progress_tracking USING progress_tracking_pkey;
