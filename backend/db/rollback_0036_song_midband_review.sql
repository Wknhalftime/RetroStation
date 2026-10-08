-- Rollback for 0036_song_midband_review.sql. Run by hand with the app stopped, in one
-- transaction:
--   psql -1 -f backend/db/rollback_0036_song_midband_review.sql <database>
-- Returns to auto_matched only the songs the migration demoted that nothing has moved since:
-- still needs_review with the migration's reason text. A song a re-check rewound or re-matched,
-- or the curator approved or rejected, keeps its new state (the re-check clears the text; an
-- approval or an artist cascade changes the status). It leaves the schema_migrations row in
-- place: 0036 stays in migrations/, and without the row the next API start would demote the same
-- songs again. Reverting the D14 PR as well is optional.
UPDATE track_identities
   SET match_status = 'auto_matched', reason_code = NULL, reason_detail = NULL
 WHERE match_status = 'needs_review'
   AND reason_code = 'LOW_CONFIDENCE'
   AND reason_detail LIKE '%auto-matched by the old mid-band, back for review (D14)';
