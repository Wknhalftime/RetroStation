-- 0015_lowercase_enum_values.sql
-- Normalize enum column values to lowercase.
-- Runs after 0014 (uses new table names).
-- LogLevel (system_logs.level) intentionally excluded — stays UPPERCASE.

UPDATE broadcast_artists SET match_status = LOWER(match_status);
ALTER TABLE broadcast_artists ALTER COLUMN match_status SET DEFAULT 'pending';

UPDATE track_identities SET match_status = LOWER(match_status);
ALTER TABLE track_identities ALTER COLUMN match_status SET DEFAULT 'pending';

UPDATE matches SET match_tier = CASE
    WHEN match_tier = 'MBID_EXACT'      THEN 'musicbrainz_id_exact'
    WHEN match_tier = 'NORMALIZATION'   THEN 'normalization'
    WHEN match_tier = 'VECTOR'          THEN 'vector'
    WHEN match_tier = 'MUSICBRAINZ_API' THEN 'musicbrainz_api'
    WHEN match_tier = 'MANUAL'          THEN 'manual'
    WHEN match_tier = 'UNKNOWN'         THEN 'unclassified'
    ELSE LOWER(match_tier) END;
ALTER TABLE matches ALTER COLUMN match_tier SET DEFAULT 'unclassified';

UPDATE matches SET target_type = CASE
    WHEN target_type = 'LibraryFile' THEN 'library_file'
    ELSE LOWER(target_type) END;

UPDATE library_files SET file_status = LOWER(file_status);
ALTER TABLE library_files ALTER COLUMN file_status SET DEFAULT 'present';

UPDATE recordings SET version_type = LOWER(version_type);
ALTER TABLE recordings ALTER COLUMN version_type SET DEFAULT 'original';

UPDATE global_mapping_rules SET target_type = CASE
    WHEN target_type = 'LibraryFile' THEN 'library_file'
    ELSE LOWER(target_type) END;
