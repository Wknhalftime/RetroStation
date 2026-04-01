-- Canonical layer: MusicBrainz entities. PKs are MBIDs (TEXT).

CREATE TABLE artists (
    id                TEXT        PRIMARY KEY,
    name              TEXT        NOT NULL,
    sort_name         TEXT        NOT NULL,
    disambiguation    TEXT,
    needs_enhancement BOOLEAN     NOT NULL DEFAULT TRUE,
    enhanced_at       TIMESTAMPTZ,
    enhancement_error TEXT
);

CREATE TABLE works (
    id                TEXT        PRIMARY KEY,
    title             TEXT        NOT NULL,
    artist_id         TEXT        NOT NULL REFERENCES artists(id),
    needs_enhancement BOOLEAN     NOT NULL DEFAULT TRUE,
    enhanced_at       TIMESTAMPTZ,
    enhancement_error TEXT
    -- embedding vector(1024) added in 0005_vector_indexes.sql
);

CREATE INDEX idx_works_artist ON works(artist_id);

CREATE TABLE recordings (
    id                TEXT        PRIMARY KEY,
    title             TEXT        NOT NULL,
    work_id           TEXT        REFERENCES works(id),
    duration_ms       INTEGER,
    version_type      TEXT        NOT NULL DEFAULT 'ORIGINAL',
    needs_enhancement BOOLEAN     NOT NULL DEFAULT TRUE,
    enhanced_at       TIMESTAMPTZ,
    enhancement_error TEXT
    -- embedding vector(1024) added in 0005_vector_indexes.sql
);

CREATE INDEX idx_recordings_work ON recordings(work_id);
