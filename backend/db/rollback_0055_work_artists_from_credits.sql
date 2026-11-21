-- Rollback of 0055_work_artists_from_credits (work merges phase 3b PR 5b, SPEC S7, D38, D49).
--
-- works.artist_id comes back nullable: from the seed while it is still there, else from the
-- first credit of the work's default performance (the one holding its master's file), by artist
-- id or the artist holding the credit's MBID. A work with neither keeps none. matches.work_id
-- comes back from each match's file's work. Both indexes return; the seed and 0055 go.

ALTER TABLE works ADD COLUMN artist_id TEXT REFERENCES artists(id);

DO $$
BEGIN
    IF to_regclass('work_artist_seed') IS NOT NULL THEN
        UPDATE works w SET artist_id = s.artist_id
          FROM work_artist_seed s
         WHERE s.work_id = w.id;
    END IF;
END $$;

UPDATE works w SET artist_id = d.artist_id
  FROM (
      SELECT sm.work_id, COALESCE(pc.artist_id, a.id) AS artist_id
        FROM song_masters sm
        JOIN library_files lf ON lf.id = sm.preferred_file_id
        JOIN performance_credits pc ON pc.performance_id = lf.performance_id AND pc.position = 0
        LEFT JOIN artists a ON pc.artist_id IS NULL AND a.mbid = pc.mbid
  ) d
 WHERE d.work_id = w.id AND w.artist_id IS NULL AND d.artist_id IS NOT NULL;

CREATE INDEX idx_works_artist ON works(artist_id);

ALTER TABLE matches ADD COLUMN work_id TEXT REFERENCES works(id);

UPDATE matches m SET work_id = COALESCE(f.work_id, r.work_id)
  FROM library_files f
  LEFT JOIN recordings r ON r.id = f.recording_id
 WHERE f.id = m.library_file_id;

CREATE INDEX idx_matches_work_id ON matches(work_id);

DROP TABLE IF EXISTS work_artist_seed;

DELETE FROM schema_migrations WHERE version = '0055_work_artists_from_credits';
