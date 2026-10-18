-- 0042_station_liners.sql
-- Station liners (station liners spec, LR1, LR3, LR4): each station's own audio clips, and
-- how many songs the station plays between them.

-- The cadence: a liner after every N songs. NULL means liners are off (LR1).
ALTER TABLE stations
    ADD COLUMN songs_between_liners INT NULL
        CONSTRAINT stations_songs_between_liners
        CHECK (songs_between_liners BETWEEN 1 AND 1000);

-- One row per liner. The file lives at <liners root>/<station_id>/<file_name>; file_name is
-- the liner's id and format. The same audio is one liner per station (the unique key), but
-- two stations each own their own copy. Cues come from stream_cues by audio_hash, like a
-- song's; a manual edit is per liner, so it lives here, not in stream_cue_edits. The edit's
-- fade-out always sits at its Stop, so three columns hold it.
CREATE TABLE station_liners (
    id           UUID        PRIMARY KEY,
    station_id   UUID        NOT NULL REFERENCES stations(id) ON DELETE CASCADE,
    name         TEXT        NOT NULL
                             CONSTRAINT station_liners_name_length
                             CHECK (char_length(name) BETWEEN 1 AND 255),
    file_name    TEXT        NOT NULL
                             CONSTRAINT station_liners_file_name_format
                             CHECK (file_name ~ (
        '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.(flac|mp3|wav)$'
                             )),
    format       TEXT        NOT NULL
                             CONSTRAINT station_liners_format
                             CHECK (format IN ('flac', 'mp3', 'wav')),
    audio_hash   TEXT        NOT NULL
                             CONSTRAINT station_liners_audio_hash_format
                             CHECK (audio_hash ~
                                 '^(flac-md5:[0-9a-f]{32}|audio-sha256:[0-9a-f]{64})$'),
    duration_ms  INT         NOT NULL
                             CONSTRAINT station_liners_duration
                             CHECK (duration_ms > 0),
    cue_start_ms INT         NULL,
    cue_eom_ms   INT         NULL,
    cue_stop_ms  INT         NULL,
    uploaded_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT station_liners_station_id_audio_hash_key UNIQUE (station_id, audio_hash),
    -- All three null, or all three set in order. Each branch tests IS NULL / IS NOT NULL
    -- explicitly: a comparison with a NULL is never false, so it would let a half-set row in.
    CONSTRAINT station_liners_cues CHECK (
        (cue_start_ms IS NULL AND cue_eom_ms IS NULL AND cue_stop_ms IS NULL)
        OR (
            cue_start_ms IS NOT NULL
            AND cue_eom_ms IS NOT NULL
            AND cue_stop_ms IS NOT NULL
            AND cue_start_ms >= 0
            AND cue_start_ms < cue_eom_ms
            AND cue_eom_ms <= cue_stop_ms
            AND cue_stop_ms <= duration_ms
        )
    )
);

-- "Does a liner carry this audio?" (the cue store's conditional write and orphan prune).
CREATE INDEX idx_station_liners_audio_hash ON station_liners (audio_hash);
