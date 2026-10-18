-- 0041_stream_cue_edits.sql
-- Manual cue edits for tune-in streaming (cue editor spec, R1, R4, D20). One row per audio,
-- keyed by the library's audio fingerprint like stream_cues, but a separate table: the
-- analyser purge (stream_cues.purge_other_versions) and the orphan prune must never delete
-- what a person edited. No FK for the same reason as stream_cues (a hash is not unique in
-- the library, and an edit for audio no longer there is harmless). The marker ordering is
-- enforced here as well as in CueMarkers, so no writer can store a row the reader rejects.
CREATE TABLE stream_cue_edits (
    audio_hash TEXT        PRIMARY KEY
                           CONSTRAINT stream_cue_edits_audio_hash_format
                           CHECK (audio_hash ~
                               '^(flac-md5:[0-9a-f]{32}|audio-sha256:[0-9a-f]{64})$'),
    start_ms   INT         NOT NULL,
    eom_ms     INT         NOT NULL,
    fade_at_ms INT         NOT NULL,  -- a position: where the fade-out begins
    stop_ms    INT         NOT NULL,
    edited_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT stream_cue_edits_order CHECK (
        start_ms >= 0
        AND start_ms < eom_ms
        AND eom_ms <= stop_ms
        AND fade_at_ms BETWEEN start_ms AND stop_ms
    )
);
