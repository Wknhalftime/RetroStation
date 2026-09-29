-- 0031_missing_since.sql
-- When each row went missing, for the Missing Files page (spec C1). mark_missing
-- sets it; every write that makes a row PRESENT again clears it (the upsert,
-- relocate). Rows missing before this migration get its time: when they really
-- went missing was never recorded. No CHECK ties it to file_status: rows written
-- by hand (test seeds, repair scripts) may be missing without a time, and the
-- page shows those as unknown.
ALTER TABLE library_files ADD COLUMN missing_since TIMESTAMPTZ;

UPDATE library_files SET missing_since = now() WHERE file_status = 'missing';

-- The Missing Files page pages missing rows in path order.
CREATE INDEX idx_library_files_missing
    ON library_files (file_path)
    WHERE file_status = 'missing';
