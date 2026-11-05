-- Artist model phase 2a (SPEC S5, S14; C41, C21; AUD-R041, AUD-R072): names are unique only
-- without an MBID, so each MBID can be its own artist. The unique index goes partial. The plain
-- index serves the readers of a name, works.py's candidate join and the local upsert's count,
-- which a partial index can't. library_suggestions holds what the Library asks the curator to
-- look at (design 6.10); phase 5 adds its page.
--
-- No rollback script from here (D4): once two artists share a name the old index can't come back,
-- and the code before 2a fails on every scan without it. The rollback is the dump (phase 7 S5).
SET LOCAL lock_timeout = '5s';

DROP INDEX IF EXISTS idx_artists_norm_name;
CREATE UNIQUE INDEX idx_artists_norm_name ON artists (normalized_name) WHERE mbid IS NULL;
CREATE INDEX idx_artists_normalized_name ON artists (normalized_name);
ANALYZE artists;

-- reason: 4.4 and 6 add theirs by migration. A row naming a file goes with the file; nothing
-- writes one in 2a. state: a repeat of a suggestion never changes it (S14).
CREATE TABLE library_suggestions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    library_file_id UUID REFERENCES library_files(id) ON DELETE CASCADE,
    normalized_name TEXT NOT NULL,
    reason          TEXT NOT NULL CHECK (reason IN ('shared_name', 'different_acts')),
    candidates      JSONB NOT NULL DEFAULT '[]',
    evidence        JSONB NOT NULL DEFAULT '{}',
    state           TEXT NOT NULL DEFAULT 'open'
                    CHECK (state IN ('open', 'accepted', 'dismissed')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX uq_library_suggestions_name_reason
    ON library_suggestions (normalized_name, reason) WHERE library_file_id IS NULL;
CREATE INDEX idx_library_suggestions_file
    ON library_suggestions (library_file_id) WHERE library_file_id IS NOT NULL;
