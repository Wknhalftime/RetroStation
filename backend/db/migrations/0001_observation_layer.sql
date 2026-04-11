-- Observation layer: raw radio log data. Never edited after ingestion.

CREATE TABLE playlists (
    id           UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    name         TEXT        NOT NULL,
    content_hash TEXT        NOT NULL UNIQUE,
    ingested_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    -- station_id UUID added in 0007_stations.sql
);

CREATE TABLE log_artists (
    id                UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    original_name     TEXT        NOT NULL,
    normalized_name   TEXT        NOT NULL UNIQUE,
    match_status      TEXT        NOT NULL DEFAULT 'pending',
    artist_candidates JSONB,
    error_message     TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
    -- embedding vector(1024) added in 0005_vector_indexes.sql
);

CREATE TABLE log_identities (
    id                   UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    artist_id            UUID        NOT NULL REFERENCES log_artists(id),
    original_title       TEXT        NOT NULL,
    normalized_title     TEXT        NOT NULL,
    normalized_signature TEXT        NOT NULL UNIQUE,
    match_status         TEXT        NOT NULL DEFAULT 'pending',
    match_tier           TEXT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
    -- embedding vector(1024) added in 0005_vector_indexes.sql
);

CREATE INDEX idx_log_identities_artist ON log_identities(artist_id);
CREATE INDEX idx_log_identities_status ON log_identities(match_status);

CREATE TABLE log_events (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    identity_id UUID        NOT NULL REFERENCES log_identities(id),
    playlist_id UUID        NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    played_at   TIMESTAMPTZ NOT NULL,
    UNIQUE (identity_id, playlist_id, played_at)
    -- broadcast_day_id UUID added in 0007_stations.sql
);

CREATE INDEX idx_log_events_playlist  ON log_events(playlist_id);
CREATE INDEX idx_log_events_identity  ON log_events(identity_id);
CREATE INDEX idx_log_events_played_at ON log_events(played_at);
