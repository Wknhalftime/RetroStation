-- pg_trgm already enabled by migration 0022; CREATE EXTENSION IF NOT EXISTS is
-- idempotent, but we rely on 0022 having run first.
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- GIN trigram partial index on LOWER(track_title) — matches the case-folding
-- the endpoint applies in WHERE / ORDER BY so the index is actually usable.
-- The WHERE guard keeps the index small (no NULL rows) and satisfies the
-- planner's requirement that the query predicate subsumes the index predicate.
CREATE INDEX IF NOT EXISTS idx_library_files_track_title_trgm
    ON library_files USING GIN (LOWER(track_title) gin_trgm_ops)
    WHERE track_title IS NOT NULL;
