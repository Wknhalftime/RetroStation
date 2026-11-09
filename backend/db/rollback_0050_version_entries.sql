-- Rollback for 0050_version_entries.sql (versions, phase 2b PR 2). Run by hand, with the app
-- stopped, in one transaction:
--   psql -1 -f backend/db/rollback_0050_version_entries.sql <database>
-- It puts back 0030's play_file_resolution and its comment, drops version_entry_states,
-- deletes every version entry (format overrides stay), drops format_overrides' version_text
-- with its CHECK and UNIQUE and makes format_name NOT NULL again, drops track_identities'
-- version_text with its index, and deletes the schema_migrations row.
SET LOCAL lock_timeout = '5s';

CREATE OR REPLACE VIEW play_file_resolution AS
SELECT pe.id AS play_event_id,
       pl.station_id,
       best.library_file_id AS matched_file_id,
       COALESCE(matched.work_id, r.work_id) AS work_id,
       f.id AS file_id,
       f.file_status,
       CASE WHEN fo.preferred_file_id IS NOT NULL THEN 'override'
            WHEN sm.preferred_file_id IS NOT NULL THEN 'master'
       END AS source
FROM play_events pe
JOIN playlists pl ON pl.id = pe.playlist_id
LEFT JOIN stations s ON s.id = pl.station_id
LEFT JOIN track_identities ti ON ti.id = pe.identity_id
LEFT JOIN LATERAL (
    SELECT m.library_file_id
    FROM matches m
    WHERE m.identity_id = pe.identity_id
      AND m.library_file_id IS NOT NULL
      AND ti.match_status IN ('auto_matched', 'manual_matched')
    ORDER BY m.confidence_score DESC, m.created_at, m.id
    LIMIT 1
) best ON true
LEFT JOIN library_files matched ON matched.id = best.library_file_id
LEFT JOIN recordings r ON r.id = matched.recording_id
LEFT JOIN song_masters sm ON sm.work_id = COALESCE(matched.work_id, r.work_id)
LEFT JOIN format_overrides fo
       ON fo.work_id = COALESCE(matched.work_id, r.work_id)
      AND fo.format_name = s.format_name
LEFT JOIN library_files f
       ON f.id = COALESCE(fo.preferred_file_id, sm.preferred_file_id);

COMMENT ON VIEW play_file_resolution IS
    'Query per play: filter on play_event_id, or join it through LATERAL (... WHERE '
    'play_event_id = x OFFSET 0). A plain join or a station-wide filter computes it for '
    'every play (about 10 s on dev, 5M plays). matched_file_id is only the route to '
    'work_id and never plays; file_id is the override or master, and source is override, '
    'master or NULL.';

DROP TABLE version_entry_states;

DELETE FROM format_overrides WHERE version_text IS NOT NULL;
ALTER TABLE format_overrides DROP CONSTRAINT format_overrides_work_id_version_text_key;
ALTER TABLE format_overrides DROP CONSTRAINT format_overrides_one_kind;
ALTER TABLE format_overrides DROP COLUMN version_text;
ALTER TABLE format_overrides ALTER COLUMN format_name SET NOT NULL;

ALTER TABLE track_identities DROP COLUMN version_text;

DELETE FROM schema_migrations WHERE version = '0050_version_entries';
