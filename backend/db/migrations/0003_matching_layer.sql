-- Matching layer: links log entries to canonical entities.
-- NOTE: matches.library_file_id has no FK here — library_files doesn't exist
-- until 0004. The FK constraint is added via ALTER TABLE in 0004.

CREATE TABLE matches (
    id               UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    identity_id      UUID        REFERENCES log_identities(id),
    artist_id        UUID        REFERENCES log_artists(id),
    library_file_id  UUID,
    target_id        TEXT,
    target_type      TEXT,
    confidence_score REAL        NOT NULL DEFAULT 0.0,
    match_tier       TEXT        NOT NULL DEFAULT 'unclassified',
    trace_id         TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT xor_match_target CHECK (
        (identity_id IS NOT NULL AND artist_id IS NULL)
        OR (identity_id IS NULL  AND artist_id IS NOT NULL)
    ),
    UNIQUE (identity_id, library_file_id),
    UNIQUE (artist_id, target_id)
);

CREATE INDEX idx_matches_identity     ON matches(identity_id);
CREATE INDEX idx_matches_artist       ON matches(artist_id);
CREATE INDEX idx_matches_library_file ON matches(library_file_id);

CREATE TABLE global_mapping_rules (
    id             UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    source_pattern TEXT        NOT NULL,
    target_type    TEXT        NOT NULL,
    target_id      TEXT        NOT NULL,
    priority       INTEGER     NOT NULL DEFAULT 0,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_rules_priority ON global_mapping_rules(priority DESC);
