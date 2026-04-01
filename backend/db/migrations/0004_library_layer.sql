-- Library layer: local audio files.

CREATE TABLE library_files (
    id                   UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    file_path            TEXT        NOT NULL UNIQUE,
    file_hash            TEXT        NOT NULL,
    indexed_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    trace_id             TEXT,
    recording_id         TEXT        REFERENCES recordings(id),
    recording_mbid       TEXT,
    artist_mbid          TEXT,
    album_artist_mbid    TEXT,
    release_mbid         TEXT,
    release_title        TEXT,
    release_type         TEXT,
    release_type_secondary TEXT,
    release_status       TEXT,
    track_title          TEXT,
    track_number         SMALLINT,
    disc_number          SMALLINT,
    duration_ms          INTEGER,
    format               TEXT        NOT NULL DEFAULT 'unknown',
    bitrate              INTEGER,
    enrichment_status    TEXT        NOT NULL DEFAULT 'pending',
    raw_metadata         JSONB
);

CREATE INDEX idx_library_files_enrichment_album
    ON library_files(enrichment_status, release_mbid)
    WHERE enrichment_status = 'pending' AND release_mbid IS NOT NULL;

CREATE INDEX idx_library_files_enrichment_recording
    ON library_files(enrichment_status, recording_mbid)
    WHERE enrichment_status = 'pending'
      AND recording_mbid IS NOT NULL
      AND release_mbid IS NULL;

CREATE INDEX idx_library_files_artist_mbid       ON library_files(artist_mbid);
CREATE INDEX idx_library_files_album_artist_mbid ON library_files(album_artist_mbid);
CREATE INDEX idx_library_files_release_mbid      ON library_files(release_mbid);
CREATE INDEX idx_library_files_recording_id      ON library_files(recording_id);

CREATE TABLE library_quarantine (
    id            UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    file_path     TEXT        NOT NULL,
    error_message TEXT        NOT NULL,
    trace_id      TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Deferred FK: library_files now exists
ALTER TABLE matches
    ADD CONSTRAINT fk_matches_library_file
    FOREIGN KEY (library_file_id) REFERENCES library_files(id);
