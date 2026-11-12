-- Credit picks, performances phase 3a PR 3 (SPEC S7, S8; design C57, C61, C64, C65, C71;
-- AUD-R033, AUD-R039, AUD-R076). The runner wraps this file in one transaction.
-- Rollback: backend/db/rollback_0052_credit_picks.sql.
SET LOCAL lock_timeout = '5s';

-- S8 (C57 as D4 amends it): one proposal per log line, for Lance to apply, skip or pick. A row
-- is decided exactly when its decided_at is set. A deleted match or performance leaves the row
-- stale (Apply skips it); a deleted line takes its row with it.
CREATE TABLE credit_pick_proposals (
    id             UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    identity_id    UUID        NOT NULL REFERENCES track_identities (id) ON DELETE CASCADE,
    match_id       UUID        REFERENCES matches (id) ON DELETE SET NULL,
    performance_id UUID        REFERENCES performances (id) ON DELETE SET NULL,
    kind           TEXT        NOT NULL,
    reason         TEXT        NOT NULL,
    candidates     UUID[]      NOT NULL DEFAULT '{}',
    state          TEXT        NOT NULL DEFAULT 'proposed',
    proposed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at     TIMESTAMPTZ,
    CONSTRAINT credit_pick_proposals_identity_id_key UNIQUE (identity_id),
    CONSTRAINT credit_pick_proposals_kind_known
        CHECK (kind IN ('credit', 'version_line', 'tie')),
    CONSTRAINT credit_pick_proposals_reason_known CHECK (reason IN ('credit', 'shared_artist')),
    CONSTRAINT credit_pick_proposals_state_known
        CHECK (state IN ('proposed', 'skipped', 'applied', 'picked')),
    CONSTRAINT credit_pick_proposals_decided_when_decided
        CHECK ((state = 'proposed') = (decided_at IS NULL))
);

CREATE INDEX idx_credit_pick_proposals_match ON credit_pick_proposals (match_id);
CREATE INDEX idx_credit_pick_proposals_performance ON credit_pick_proposals (performance_id);

-- S7 (C61, C71, AUD-R033): the stored performance first, then the version entry, the format
-- override and the master. The stored performance comes from the best match the view already
-- picks, key-joined on (id, the line's work), and is skipped when it is the default (the
-- performance of the master's file). The columns are unchanged.
CREATE OR REPLACE VIEW play_file_resolution AS
SELECT pe.id AS play_event_id,
       pl.station_id,
       best.library_file_id AS matched_file_id,
       COALESCE(matched.work_id, r.work_id) AS work_id,
       f.id AS file_id,
       f.file_status,
       CASE WHEN sp.preferred_copy_id IS NOT NULL THEN 'performance'
            WHEN ve.preferred_file_id IS NOT NULL THEN 'version'
            WHEN fo.preferred_file_id IS NOT NULL THEN 'override'
            WHEN sm.preferred_file_id IS NOT NULL THEN 'master'
       END AS source
FROM play_events pe
JOIN playlists pl ON pl.id = pe.playlist_id
LEFT JOIN stations s ON s.id = pl.station_id
LEFT JOIN track_identities ti ON ti.id = pe.identity_id
LEFT JOIN LATERAL (
    SELECT m.library_file_id, m.performance_id
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
LEFT JOIN library_files mf ON mf.id = sm.preferred_file_id
LEFT JOIN performances sp
       ON sp.id = best.performance_id
      AND sp.work_id = COALESCE(matched.work_id, r.work_id)
      AND sp.id IS DISTINCT FROM mf.performance_id
LEFT JOIN format_overrides ve
       ON ve.work_id = COALESCE(matched.work_id, r.work_id)
      AND ve.version_text = ti.version_text
LEFT JOIN format_overrides fo
       ON fo.work_id = COALESCE(matched.work_id, r.work_id)
      AND fo.format_name = s.format_name
LEFT JOIN library_files f
       ON f.id = COALESCE(
           sp.preferred_copy_id, ve.preferred_file_id, fo.preferred_file_id, sm.preferred_file_id
       );

COMMENT ON VIEW play_file_resolution IS
    'Query per play: filter on play_event_id, or join it through LATERAL (... WHERE '
    'play_event_id = x OFFSET 0). A plain join or a station-wide filter computes it for '
    'every play (about 10 s on dev, 5M plays). matched_file_id is only the route to '
    'work_id and never plays; file_id is the preferred copy of the performance the best '
    'match stores (unless it is the default), else the version entry for the line''s '
    'version text, else the station format''s override, else the master, and source is '
    'performance, version, override, master or NULL.';

ANALYZE credit_pick_proposals;
