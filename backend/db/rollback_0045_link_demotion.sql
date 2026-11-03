-- Rollback for 0045_link_demotion.sql (artist model phase 1c, SPEC S2). Run by hand, with the
-- app stopped, in one transaction, before the code goes back to the base before 1c:
--   psql -1 -f backend/db/rollback_0045_link_demotion.sql <database>
-- From link_demotion_backup, it puts back every link and name status 0045 changed or deleted,
-- unless it moved since: a link or a status that no longer holds what 0045 wrote keeps its new
-- state (a suggestion the curator accepted, a pair the matcher linked again: the later decision
-- wins). A re-check that rewound a name, or rewound it and matched it again to the same status
-- and reason, is no decision, so that name is put back too. Then the reasons the code before 1c
-- cannot read go, and it drops the backup and the placeholder list and deletes 0045's
-- schema_migrations row. It takes no lock on broadcast_artists above ROW EXCLUSIVE. After phase
-- 1c's 0046, roll back newest first: 0046's rollback, this one, then 0044's (phase 7's runbook).

-- Links 0045 changed, still as it left them: their state back.
UPDATE log_name_links l
   SET state = b.before ->> 'state',
       updated_at = (b.before ->> 'updated_at')::timestamptz
  FROM link_demotion_backup b
 WHERE b.kind = 'link'
   AND b.after IS NOT NULL
   AND l.id = b.id
   AND l.state = b.after ->> 'state'
   AND l.origin = b.after ->> 'origin'
   AND l.strength = (b.after ->> 'strength')::smallint
   AND l.evidence = b.after -> 'evidence';

-- Links 0045 deleted: back, with their id and created_at, unless the pair has a row again (or
-- its name or artist is gone since).
INSERT INTO log_name_links
SELECT r.*
  FROM link_demotion_backup b
 CROSS JOIN LATERAL jsonb_populate_record(NULL::log_name_links, b.before) r
 WHERE b.kind = 'link'
   AND b.after IS NULL
   AND EXISTS (SELECT 1 FROM broadcast_artists ba WHERE ba.id = r.broadcast_artist_id)
   AND EXISTS (SELECT 1 FROM artists a WHERE a.id = r.artist_id)
   AND NOT EXISTS (
       SELECT 1 FROM log_name_links l
        WHERE l.broadcast_artist_id = r.broadcast_artist_id AND l.artist_id = r.artist_id
   );

-- Statuses 0045 wrote that still hold its status and reason (the words may be the matcher's,
-- after a re-check), or that a re-check rewound to pending since: the name's status before.
UPDATE broadcast_artists ba
   SET match_status  = b.before ->> 'match_status',
       reason_code   = b.before ->> 'reason_code',
       reason_detail = b.before ->> 'reason_detail'
  FROM link_demotion_backup b
 WHERE b.kind = 'name'
   AND b.id = ba.id
   AND (
       (ba.match_status = b.after ->> 'match_status'
        AND ba.reason_code IS NOT DISTINCT FROM b.after ->> 'reason_code')
       OR (ba.match_status = 'pending' AND ba.reason_code IS NULL)
   );

-- The code before 1c reads neither reason (its ReasonCode has neither): a name still holding
-- one, written by 1c's matcher on a name 0045 never touched, goes back to pending for the old
-- matcher to decide again.
UPDATE broadcast_artists
   SET match_status = 'pending', reason_code = NULL, reason_detail = NULL
 WHERE reason_code IN ('LINK_SUGGESTED', 'LINK_REMOVED');

DROP TABLE IF EXISTS log_name_placeholders;
DROP TABLE IF EXISTS link_demotion_backup;
DELETE FROM schema_migrations WHERE version = '0045_link_demotion';
