-- 0024_matches_work_id.sql
-- Persist work_id on matches so manual + auto resolutions both feed
-- master_selection_service.recalculate_song_masters. Mirrors the
-- work_id columns already present on library_files (0011) and
-- song_masters (0008).

ALTER TABLE matches ADD COLUMN work_id TEXT REFERENCES works(id);

CREATE INDEX idx_matches_work_id ON matches(work_id);

-- Backfill: prefer library_files.work_id; fall back to recordings.work_id
-- via the recording_id linkage so legacy rows with NULL lf.work_id but a
-- live recording linkage still get populated.
UPDATE matches m
SET work_id = COALESCE(lf.work_id, r.work_id)
FROM library_files lf
LEFT JOIN recordings r ON r.id = lf.recording_id
WHERE m.library_file_id = lf.id
  AND COALESCE(lf.work_id, r.work_id) IS NOT NULL;
