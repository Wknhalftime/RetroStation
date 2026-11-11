-- Performances and credits, phase 3a PR 2 (SPEC S1, D1; design C40, C44, C71; AUD-R032).
-- The runner wraps this file in one transaction. Nothing fills the tables here: the run-once
-- build task does (S2), and every writer keeps them (S3).
SET LOCAL lock_timeout = '5s';

-- C40: one recording of a work, with its version and length; an optional recording MBID,
-- unique within the work where present (3b builds the global index after its merges).
CREATE TABLE performances (
    id                UUID    PRIMARY KEY DEFAULT gen_random_uuid(),
    work_id           TEXT    NOT NULL REFERENCES works(id) ON DELETE CASCADE,
    recording_mbid    TEXT,
    version_type      TEXT    NOT NULL,
    length_ms         INTEGER,
    preferred_copy_id UUID,
    CONSTRAINT performances_id_work_id_key UNIQUE (id, work_id)
);

CREATE UNIQUE INDEX uq_performances_work_recording_mbid
    ON performances (work_id, recording_mbid) WHERE recording_mbid IS NOT NULL;
CREATE INDEX idx_performances_work_id ON performances (work_id);
CREATE INDEX idx_performances_preferred_copy_id ON performances (preferred_copy_id);

-- C44, D1: a credit names an artist, or an MBID no artist holds yet (readers join artists on
-- the MBID), with its credited name and the joining words after it.
CREATE TABLE performance_credits (
    performance_id UUID     NOT NULL REFERENCES performances(id) ON DELETE CASCADE,
    position       SMALLINT NOT NULL,
    artist_id      TEXT     REFERENCES artists(id),
    mbid           TEXT,
    credited_name  TEXT,
    join_after     TEXT,
    role           TEXT     NOT NULL CHECK (role IN ('main', 'featured')),
    PRIMARY KEY (performance_id, position),
    CONSTRAINT performance_credits_names_someone
        CHECK (artist_id IS NOT NULL OR mbid IS NOT NULL)
);

CREATE INDEX idx_performance_credits_artist_id ON performance_credits (artist_id);
CREATE INDEX idx_performance_credits_mbid ON performance_credits (mbid);

-- C71: a file and its performance share a work. Checked per statement, so a writer that
-- moves a file must clear performance_id in the same UPDATE; MATCH SIMPLE skips a NULL pair,
-- hence the CHECK.
ALTER TABLE library_files ADD COLUMN performance_id UUID;
ALTER TABLE library_files
    ADD CONSTRAINT library_files_performance_work_fkey
    FOREIGN KEY (performance_id, work_id) REFERENCES performances (id, work_id)
    DEFERRABLE INITIALLY IMMEDIATE;
ALTER TABLE library_files
    ADD CONSTRAINT library_files_performance_needs_work
    CHECK (performance_id IS NULL OR work_id IS NOT NULL);
ALTER TABLE library_files
    ADD CONSTRAINT library_files_id_performance_id_key UNIQUE (id, performance_id);
CREATE INDEX idx_library_files_performance_work ON library_files (performance_id, work_id);

-- C71: a performance's preferred copy is one of its own files. A deleted copy leaves none,
-- and the path that deleted it re-picks (S5); a copy can't leave before it is re-picked.
ALTER TABLE performances
    ADD CONSTRAINT performances_preferred_copy_fkey
    FOREIGN KEY (preferred_copy_id, id) REFERENCES library_files (id, performance_id)
    ON DELETE SET NULL (preferred_copy_id);

-- M28: a line stores a performance only for a credit; a deleted performance leaves none.
ALTER TABLE matches
    ADD COLUMN performance_id UUID REFERENCES performances(id) ON DELETE SET NULL;
CREATE INDEX idx_matches_performance_id ON matches (performance_id);

ANALYZE performances;
ANALYZE performance_credits;
ANALYZE library_files;
ANALYZE matches;
