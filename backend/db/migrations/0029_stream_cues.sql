-- 0029_stream_cues.sql
-- Cached playback analysis per library file for tune-in streaming (spec: Data).
-- A row is fresh only while file_size/file_mtime_ns match library_files and
-- analyser_version is the current one; the schedule reader treats anything else
-- as "no cues". No CHECK constraints: the write model validates, and the reader
-- tolerates a bad row (cues=None, logged) rather than failing a whole day.

CREATE TABLE stream_cues (
    library_file_id  UUID        PRIMARY KEY
                                 REFERENCES library_files(id) ON DELETE CASCADE,
    cue_in_ms        INT         NOT NULL,
    cue_out_ms       INT         NOT NULL,
    fade_in_ms       INT         NOT NULL,
    fade_out_ms      INT         NOT NULL,
    start_next_ms    INT         NOT NULL,  -- counted back from cue_out_ms
    loudness_lufs    REAL,
    gain_db          REAL        NOT NULL,  -- gain to reach -18 LUFS
    file_size        BIGINT,
    file_mtime_ns    BIGINT,
    analyser_version INT         NOT NULL,
    analysis_failed  BOOLEAN     NOT NULL DEFAULT false,
    analysed_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
