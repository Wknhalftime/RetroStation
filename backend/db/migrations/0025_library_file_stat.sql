-- 0025_library_file_stat.sql
-- Size + mtime per library file, so an incremental scan can skip an
-- unchanged file on a stat() instead of reading every byte to re-hash it.
-- NULL on existing rows means "not yet recorded"; the scanner backfills a
-- row the first time it visits the file and finds it unchanged.

ALTER TABLE library_files ADD COLUMN file_size     BIGINT;
ALTER TABLE library_files ADD COLUMN file_mtime_ns BIGINT;

-- folder_hash now covers only a folder's own audio files. It used to fold
-- in child hashes, so any change lit up every ancestor to the root, the
-- change list collapsed to the root, and the non-recursive targeted scan
-- of the root indexed nothing while committing the new baseline. NULL
-- every baseline so each folder is re-verified once under the new scheme;
-- with file_size/file_mtime_ns that re-check is a stat, not a read.
UPDATE library_folders SET folder_hash = NULL;
DELETE FROM library_folder_staged_hashes;
