-- 0049_stream_endings.sql
-- Song endings for the EOM ending rules (spec docs/superpowers/specs/2026-11-07-eom-ending-rules-
-- design.md). One row per audio, keyed by the library's audio fingerprint like stream_cues, with
-- no foreign key for the same reason: a hash is not unique in the library. A separate table, so
-- an ending analysis change purges only its own rows and autocue never re-runs.
-- ending: cold, long_tail, other, or unmeasured (the audio could not be read). eom_ms is the
-- measured long-tail EOM, recorded for stage 2 and not played. measured_cue_out_ms is autocue's
-- Stop when the ending was measured: a reader applies the rule only while stream_cues still has
-- that Stop.
SET LOCAL lock_timeout = '5s';

CREATE TABLE stream_endings (
    audio_hash          TEXT        PRIMARY KEY
                                    CONSTRAINT stream_endings_audio_hash_format
                                    CHECK (audio_hash ~
                                        '^(flac-md5:[0-9a-f]{32}|audio-sha256:[0-9a-f]{64})$'),
    ending              TEXT        NOT NULL
                                    CONSTRAINT stream_endings_ending
                                    CHECK (ending IN ('cold', 'long_tail', 'other', 'unmeasured')),
    eom_ms              INT         NULL,
    measured_cue_out_ms INT         NOT NULL CHECK (measured_cue_out_ms >= 0),
    analyser_version    INT         NOT NULL CHECK (analyser_version >= 1),
    analysed_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT stream_endings_eom_only_on_long_tail CHECK (
        (ending = 'long_tail') = (eom_ms IS NOT NULL) AND (eom_ms IS NULL OR eom_ms >= 0)
    )
);
