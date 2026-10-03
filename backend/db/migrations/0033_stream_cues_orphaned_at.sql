-- 0033_stream_cues_orphaned_at.sql
-- The two-strike prune's mark (D56, D66). NULL means not orphaned. It is set by the daily
-- prune, and cleared by every upsert and when the hash reappears.
ALTER TABLE stream_cues ADD COLUMN orphaned_at TIMESTAMPTZ NULL;
