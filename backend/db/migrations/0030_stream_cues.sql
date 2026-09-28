-- 0030_stream_cues.sql
-- Tune-in streaming's cue cache, plus the two views the domain-overlap audit (D16-D21)
-- moved resolution and station-day logic into. One migration because the views and the
-- expression index they rely on all ship together; the rollback undoes all of it.

-- Playback analysis per audio for tune-in streaming (spec: Data, D20). Keyed by the
-- library's audio fingerprint: a row means "this audio has cues". No FK (a hash is not
-- unique in the library, and a row for audio no longer there is harmless). No file stat and
-- no read-time version check: analyser_version is a record only. Policy (D20): an analyser
-- change purges old rows once; that purge belongs to a later PR, not to this schema.
-- No CHECK on the cue columns themselves: the write model validates, and the
-- reader tolerates a bad row (cues=None, logged) rather than failing a whole day.
CREATE TABLE stream_cues (
    audio_hash       TEXT        PRIMARY KEY
                                 CONSTRAINT stream_cues_audio_hash_format
                                 CHECK (audio_hash ~
                                     '^(flac-md5:[0-9a-f]{32}|audio-sha256:[0-9a-f]{64})$'),
    cue_in_ms        INT         NOT NULL,
    cue_out_ms       INT         NOT NULL,
    fade_in_ms       INT         NOT NULL,
    fade_out_ms      INT         NOT NULL,
    start_next_ms    INT         NOT NULL,  -- counted back from cue_out_ms
    loudness_lufs    REAL,
    gain_db          REAL        NOT NULL,  -- gain to reach -18 LUFS
    analyser_version INT         NOT NULL,
    analysis_failed  BOOLEAN     NOT NULL DEFAULT false,
    analysed_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Curation owns "which file plays" (D16, D17): one row per play event, the final file's
-- id, file_status and source (direct / master / override). No status filter: consumers
-- decide what an unavailable final file means. Unresolved plays get a row of NULLs.
CREATE VIEW play_file_resolution AS
SELECT pe.id AS play_event_id,
       pl.station_id,
       f.id AS file_id,
       f.file_status,
       CASE WHEN f.id IS NULL THEN NULL
            WHEN fo.preferred_file_id IS NOT NULL THEN 'override'
            WHEN sm.preferred_file_id IS NOT NULL THEN 'master'
            ELSE 'direct'
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
LEFT JOIN library_files direct ON direct.id = best.library_file_id
LEFT JOIN recordings r ON r.id = direct.recording_id
LEFT JOIN song_masters sm ON sm.work_id = COALESCE(direct.work_id, r.work_id)
LEFT JOIN format_overrides fo
       ON fo.work_id = COALESCE(direct.work_id, r.work_id)
      AND fo.format_name = s.format_name
LEFT JOIN library_files f
       ON f.id = COALESCE(fo.preferred_file_id, sm.preferred_file_id, direct.id);

-- Broadcast owns "a station's plays on a date" (D19): play_date is the UTC-label date
-- (D3), independent of the session TimeZone; logged_at is the naive wall-clock time;
-- position is the 0-based index in the station-day under the D4 total order. No file
-- columns: resolution belongs to play_file_resolution alone (D17).
CREATE VIEW station_day_plays AS
SELECT pe.id AS play_event_id,
       pl.station_id,
       (pe.played_at AT TIME ZONE 'UTC')::date AS play_date,
       pe.played_at AT TIME ZONE 'UTC' AS logged_at,
       pe.identity_id,
       row_number() OVER (
           PARTITION BY pl.station_id, (pe.played_at AT TIME ZONE 'UTC')::date
           ORDER BY pe.played_at, pe.identity_id, pe.id
       ) - 1 AS position
FROM play_events pe
JOIN playlists pl ON pl.id = pe.playlist_id;

-- station_day_plays filters on play_date: index the identical expression so it is sargable
-- (D19), independent of the session TimeZone.
CREATE INDEX idx_play_events_play_date
    ON play_events (((played_at AT TIME ZONE 'UTC')::date));
