-- 0027_library_file_path_lower.sql
-- Case-only rename detection looks rows up by path ignoring case: on a
-- case-insensitive disk "Track.mp3" and "track.mp3" name the same file.
CREATE INDEX idx_library_files_path_lower
    ON library_files (lower(file_path));
