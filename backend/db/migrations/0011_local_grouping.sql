-- 0011_local_grouping.sql
-- Local-first song grouping: extend artists/works for local creation,
-- add matching columns to library_files.

-- 1. Artists: support local creation
ALTER TABLE artists ADD COLUMN mbid TEXT UNIQUE;
ALTER TABLE artists ADD COLUMN origin TEXT NOT NULL DEFAULT 'local';
ALTER TABLE artists ADD COLUMN normalized_name TEXT;

-- Backfill existing MB-sourced rows
UPDATE artists SET mbid = id, origin = 'musicbrainz';

-- Backfill normalized_name from name
UPDATE artists SET normalized_name = lower(trim(name))
WHERE normalized_name IS NULL;

-- Unique index for concurrent-safe artist creation
CREATE UNIQUE INDEX idx_artists_norm_name ON artists(normalized_name);

-- 2. Works: support local creation
ALTER TABLE works ADD COLUMN mbid TEXT UNIQUE;
ALTER TABLE works ADD COLUMN origin TEXT NOT NULL DEFAULT 'local';

-- Backfill existing MB-sourced rows
UPDATE works SET mbid = id, origin = 'musicbrainz';

-- 3. Library files: matching columns + direct work link
ALTER TABLE library_files ADD COLUMN artist_name TEXT;
ALTER TABLE library_files ADD COLUMN normalized_artist_name TEXT;
ALTER TABLE library_files ADD COLUMN normalized_title TEXT;
ALTER TABLE library_files ADD COLUMN work_id TEXT REFERENCES works(id);

-- Indexes for matching
CREATE INDEX idx_library_files_file_hash ON library_files(file_hash);
CREATE INDEX idx_library_files_norm_artist ON library_files(normalized_artist_name);
CREATE INDEX idx_library_files_work_id ON library_files(work_id);

-- 4. Backfill work_id for already-enriched files
UPDATE library_files lf
SET work_id = r.work_id
FROM recordings r
WHERE lf.recording_id = r.id
  AND r.work_id IS NOT NULL;
