-- 0029_audio_hash.sql
-- A fingerprint of each file's audio alone, so a file moved and retagged in
-- one go is still recognised. '<kind>:<lowercase hex>': flac-md5 (the MD5 a
-- FLAC encoder stores in STREAMINFO) or audio-sha256 (SHA-256 of the audio
-- bytes without tags). NULL until a scan (flac-md5) or
-- library_hash_backfill_task fills it in. file_hash stays until this has run
-- on the dev library; a follow-up migration drops it.
ALTER TABLE library_files ADD COLUMN audio_hash TEXT
    CONSTRAINT library_files_audio_hash_format
    CHECK (audio_hash ~ '^(flac-md5:[0-9a-f]{32}|audio-sha256:[0-9a-f]{64})$');

-- Move detection and missing-file reconciliation look rows up by audio hash.
CREATE INDEX idx_library_files_audio_hash
    ON library_files (audio_hash)
    WHERE audio_hash IS NOT NULL;

-- The backfill's work queue, in the order it reads files. Only formats that
-- have a fingerprint, so rows it can never hash do not keep it busy.
CREATE INDEX idx_library_files_audio_unhashed
    ON library_files (file_path)
    WHERE audio_hash IS NULL AND file_status = 'present' AND format IN ('flac', 'mp3');

-- Move detection looks rows up by size and mtime whatever their hash state.
CREATE INDEX idx_library_files_stat
    ON library_files (file_size, file_mtime_ns);
