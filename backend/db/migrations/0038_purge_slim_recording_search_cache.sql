-- Cached recording-search hits were slimmed without the recording's disambiguation, the only
-- live marker a search hit carries ("live, 1988-08-19"). Enrichment now reads it to store a
-- live recording as live; a stale hit (10-year TTL) would store it as original again. Drop
-- them: the next batch refetches 100 recordings per request and caches the new shape.
DELETE FROM mb_cache WHERE entity_type = 'recording-search';
