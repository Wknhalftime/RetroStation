-- Supporting partial indexes for the DEFERRED_RETRY predicate paths used by
-- the new bulk repo methods (Task 5):
--   bulk_defer_by_artist (PENDING under artist)
--   reset_deferred_by_artist_ids (NEEDS_REVIEW + DEFERRED_RETRY under artist)
--
-- The artist-side reset is now ID-keyed via the PK, so no dedicated artist-side
-- index is needed at expected sizes (see plan §"Out of Scope" — revisit if
-- deferred_artist_ids per playlist routinely exceeds ~1k).
--
-- NO BEGIN/COMMIT — backend/db/migrations.py wraps every .sql file in
-- conn.transaction() per .claude/CLAUDE.md.
--
-- Stored enum-value conventions (verified against backend/domain/enums.py):
--   MatchStatus values are lowercase  ('pending', 'needs_review', ...)
--   ReasonCode values are uppercase   ('DEFERRED_RETRY', 'LOW_CONFIDENCE', ...)
-- The mixed-case literals below are deliberate and must match the actual
-- column representation, not the Python identifier case.

-- Composite for bulk_defer_by_artist: (broadcast_artist_id, match_status).
-- The existing PK on track_identities does not help filter by FK + status
-- without a scan.
CREATE INDEX IF NOT EXISTS idx_track_identities_artist_status
    ON track_identities (broadcast_artist_id, match_status);

-- Partial index for reset_deferred_by_artist_ids — NEEDS_REVIEW + DEFERRED_RETRY
-- only, keeps the index compact.
CREATE INDEX IF NOT EXISTS idx_track_identities_deferred_artist
    ON track_identities (broadcast_artist_id)
    WHERE match_status = 'needs_review'
      AND reason_code = 'DEFERRED_RETRY';
