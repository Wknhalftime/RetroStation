-- Work merges phase 3b PR 5b (SPEC S7, D38; C70; AUD-R082, AUD-R083, AUD-R088): a work's
-- artists are its performances' credits, and a line's work is its file's work.
--
-- works.artist_id and matches.work_id retire. At the cutover every migration runs before 3a's
-- build and D1's step, which still need each existing work's artist for performances with no
-- credit, so each work's artist is copied first into work_artist_seed (D38). Only the build and
-- D1's step read it, and D1's step drops it in the transaction that sets its marker (D49).
-- An artist deleted meanwhile takes its seed rows with it.

SET LOCAL lock_timeout = '5s';

CREATE TABLE work_artist_seed (
    work_id   TEXT PRIMARY KEY REFERENCES works(id) ON DELETE CASCADE,
    artist_id TEXT NOT NULL REFERENCES artists(id) ON DELETE CASCADE
);

INSERT INTO work_artist_seed (work_id, artist_id)
SELECT id, artist_id FROM works;
ANALYZE work_artist_seed;

DROP INDEX idx_works_artist;
ALTER TABLE works DROP COLUMN artist_id;

DROP INDEX idx_matches_work_id;
ALTER TABLE matches DROP COLUMN work_id;
