-- Versions, phase 2b (SPEC S1, S2, S4, S9; design C59-C62; AUD-R033, AUD-R034).
-- The runner wraps this file in one transaction. Run it with the worker stopped: the stored
-- column rewrites track_identities (184 MB with its embeddings on 10-28) once.
SET LOCAL lock_timeout = '5s';

-- S1 (C59): a log line's version text is its title's trailing bracket groups, round or
-- square, lowercased with runs of whitespace made one space, joined in order by one space;
-- NULL without one. Generated and stored, so every line has it from the moment it exists and
-- no writer changes. Immutable functions only.
ALTER TABLE track_identities
    ADD COLUMN version_text TEXT GENERATED ALWAYS AS (
        NULLIF(
            btrim(
                regexp_replace(
                    lower(
                        regexp_replace(
                            substring(
                                original_title
                                FROM '((?:\s*(?:\([^()\[\]]*\)|\[[^()\[\]]*\]))+)\s*$'
                            ),
                            '[()\[\]]', ' ', 'g'
                        )
                    ),
                    '\s+', ' ', 'g'
                )
            ),
            ''
        )
    ) STORED;

CREATE INDEX idx_track_identities_version_text ON track_identities (version_text);

-- S2 (C60): a version entry is a format_overrides row with a version text and no format.
ALTER TABLE format_overrides ADD COLUMN version_text TEXT;
ALTER TABLE format_overrides ALTER COLUMN format_name DROP NOT NULL;
ALTER TABLE format_overrides
    ADD CONSTRAINT format_overrides_one_kind
    CHECK ((format_name IS NULL) <> (version_text IS NULL));
ALTER TABLE format_overrides
    ADD CONSTRAINT format_overrides_work_id_version_text_key UNIQUE (work_id, version_text);

-- S2 (C62, D3): what a mapping can't hold, per (work, version text).
CREATE TABLE version_entry_states (
    work_id      TEXT        NOT NULL REFERENCES works(id) ON DELETE CASCADE,
    version_text TEXT        NOT NULL,
    state        TEXT        CHECK (state = 'dismissed'),
    seen_at      TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (work_id, version_text)
);

-- S4 (C61, AUD-R033): the version entry for (work, line's version text), then the format
-- override for the station's format, then the master. The version step is a second join of
-- format_overrides by key; the columns are unchanged.
CREATE OR REPLACE VIEW play_file_resolution AS
SELECT pe.id AS play_event_id,
       pl.station_id,
       best.library_file_id AS matched_file_id,
       COALESCE(matched.work_id, r.work_id) AS work_id,
       f.id AS file_id,
       f.file_status,
       CASE WHEN ve.preferred_file_id IS NOT NULL THEN 'version'
            WHEN fo.preferred_file_id IS NOT NULL THEN 'override'
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
LEFT JOIN format_overrides ve
       ON ve.work_id = COALESCE(matched.work_id, r.work_id)
      AND ve.version_text = ti.version_text
LEFT JOIN format_overrides fo
       ON fo.work_id = COALESCE(matched.work_id, r.work_id)
      AND fo.format_name = s.format_name
LEFT JOIN library_files f
       ON f.id = COALESCE(ve.preferred_file_id, fo.preferred_file_id, sm.preferred_file_id);

COMMENT ON VIEW play_file_resolution IS
    'Query per play: filter on play_event_id, or join it through LATERAL (... WHERE '
    'play_event_id = x OFFSET 0). A plain join or a station-wide filter computes it for '
    'every play (about 10 s on dev, 5M plays). matched_file_id is only the route to '
    'work_id and never plays; file_id is the version entry for the line''s version text, '
    'else the station format''s override, else the master, and source is version, '
    'override, master or NULL.';

-- S9: silenced at start. Every (work, version text) on a matched line now is seen, so the
-- new-version marker (S8) starts only for versions that turn up later. A matched line is the
-- view's: auto or manual matched, its best match with a file, that file's work, else its
-- recording's.
INSERT INTO version_entry_states (work_id, version_text, state, seen_at)
SELECT DISTINCT COALESCE(f.work_id, r.work_id), ti.version_text, NULL::TEXT, now()
FROM track_identities ti
CROSS JOIN LATERAL (
    SELECT m.library_file_id
    FROM matches m
    WHERE m.identity_id = ti.id
      AND m.library_file_id IS NOT NULL
    ORDER BY m.confidence_score DESC, m.created_at, m.id
    LIMIT 1
) best
JOIN library_files f ON f.id = best.library_file_id
LEFT JOIN recordings r ON r.id = f.recording_id
WHERE ti.match_status IN ('auto_matched', 'manual_matched')
  AND ti.version_text IS NOT NULL
  AND COALESCE(f.work_id, r.work_id) IS NOT NULL;

ANALYZE track_identities;
ANALYZE format_overrides;
ANALYZE version_entry_states;
