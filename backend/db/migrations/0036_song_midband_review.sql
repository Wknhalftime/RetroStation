-- 0036_song_midband_review.sql
-- AUD-R025 (D14), a one-time exception to AUD-R022 D2 ("never touch auto_matched"): the songs
-- the old song mid-band auto-matched go back to review once. Under AUD-R024 D12 no song
-- auto-matches below 65 (mapping rules write 100, the strong rules start at 80), so a song
-- auto_matched with its best match row under 65 can only come from the old mid-band (dev DB,
-- 2026-10-07: 603 songs, all at 55-64). They become needs_review / LOW_CONFIDENCE and keep their
-- match tier and match row, which the review screen shows as the suggestion. The first full
-- re-check after it re-scores them: 56-64 stays in review, under 56 is auto_rejected
-- (AUD-R022 D6), and a better file can auto-match. Artists, every other status and the match
-- rows are untouched. The reason text tells the curator why the song is back, and marks the
-- rows the rollback restores.
UPDATE track_identities ti
   SET match_status  = 'needs_review',
       reason_code   = 'LOW_CONFIDENCE',
       reason_detail = 'Score ' || floor(best.score::float8 + 0.5)::int || '% ' || chr(8212)
                       || ' auto-matched by the old mid-band, back for review (D14)'
  FROM (SELECT identity_id, max(confidence_score) AS score
          FROM matches
         WHERE identity_id IS NOT NULL
         GROUP BY identity_id) best
 WHERE best.identity_id = ti.id
   AND ti.match_status = 'auto_matched'
   AND best.score < 65;
