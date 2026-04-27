-- Trigram-based fuzzy search on artists.name for the Resolution Center's
-- "Search Library" slide-over. Without trigram ranking, transcription typos
-- (e.g. "UntraSpank" -> "Ultraspank") never surface via the existing
-- substring LIKE used by /api/v1/library/artists?search=.
--
-- The endpoint switches to similarity()-based ranking with a 0.3 threshold,
-- combined with a prefix LIKE arm so 1-2 character queries still return
-- results (those fall under the trigram threshold by construction).
--
-- NO BEGIN/COMMIT -- backend/db/migrations.py wraps every .sql file in
-- conn.transaction() per .claude/CLAUDE.md.

CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- GIN trigram index on LOWER(name) -- matches the case-folding the endpoint
-- applies in WHERE / ORDER BY so the index is actually usable.
CREATE INDEX IF NOT EXISTS idx_artists_name_trgm
    ON artists USING GIN (LOWER(name) gin_trgm_ops);
