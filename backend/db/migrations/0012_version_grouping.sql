-- Work deduplication: enforce one Recording per (work_id, version_type).
-- Enables INSERT ... ON CONFLICT for get_or_create_local in grouping.
CREATE UNIQUE INDEX IF NOT EXISTS uq_recordings_work_version
    ON recordings (work_id, version_type);
