-- 0015_rollback_lowercase_enum_values.sql
-- Inverse of 0015: restore UPPERCASE enum values.
-- Run manually if rollback is needed. NOT auto-applied by the migration runner.

UPDATE broadcast_artists SET match_status = UPPER(match_status);
ALTER TABLE broadcast_artists ALTER COLUMN match_status SET DEFAULT 'PENDING';

UPDATE track_identities SET match_status = UPPER(match_status);
ALTER TABLE track_identities ALTER COLUMN match_status SET DEFAULT 'PENDING';

UPDATE matches SET match_tier = CASE
    WHEN match_tier = 'musicbrainz_id_exact' THEN 'MBID_EXACT'
    WHEN match_tier = 'normalization'        THEN 'NORMALIZATION'
    WHEN match_tier = 'vector'               THEN 'VECTOR'
    WHEN match_tier = 'musicbrainz_api'      THEN 'MUSICBRAINZ_API'
    WHEN match_tier = 'manual'               THEN 'MANUAL'
    WHEN match_tier = 'unclassified'         THEN 'UNKNOWN'
    ELSE UPPER(match_tier) END;
ALTER TABLE matches ALTER COLUMN match_tier SET DEFAULT 'UNKNOWN';

UPDATE matches SET target_type = CASE
    WHEN target_type = 'library_file' THEN 'LibraryFile'
    ELSE initcap(target_type) END;

UPDATE library_files SET file_status = UPPER(file_status);
ALTER TABLE library_files ALTER COLUMN file_status SET DEFAULT 'PRESENT';

UPDATE recordings SET version_type = UPPER(version_type);
ALTER TABLE recordings ALTER COLUMN version_type SET DEFAULT 'ORIGINAL';

UPDATE global_mapping_rules SET target_type = CASE
    WHEN target_type = 'library_file' THEN 'LibraryFile'
    ELSE initcap(target_type) END;
