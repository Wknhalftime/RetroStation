-- MusicBrainz API response cache. Prevents redundant API calls.

CREATE TABLE mb_cache (
    id            UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    cache_key     TEXT        NOT NULL UNIQUE,
    entity_type   TEXT        NOT NULL,
    entity_mbid   TEXT        NOT NULL,
    response_data JSONB       NOT NULL,
    cached_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at    TIMESTAMPTZ NOT NULL
);

CREATE INDEX idx_mb_cache_entity ON mb_cache(entity_type, entity_mbid);
CREATE INDEX idx_mb_cache_expiry ON mb_cache(expires_at);
