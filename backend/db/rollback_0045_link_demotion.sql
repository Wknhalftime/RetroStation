-- Rollback for 0045_link_demotion.sql (artist model phase 1c, SPEC S2). Run by hand, with the
-- app stopped, in one transaction:
--   psql -1 -f backend/db/rollback_0045_link_demotion.sql <database>
-- From link_demotion_backup, it puts back every link and name status 0045 changed or deleted,
-- unless it moved since: a link or a status that no longer holds what 0045 wrote keeps its new
-- state (a suggestion the curator accepted, a pair the matcher linked again: the later decision
-- wins). Then it drops the backup and the placeholder list and deletes 0045's schema_migrations
-- row. It takes no lock on broadcast_artists above ROW EXCLUSIVE. After phase 1c's 0046, roll
-- back newest first: 0046's rollback, this one, then 0044's (phase 7's runbook).

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

-- Links 0045 deleted: back, with their id and created_at, unless the pair has a row again.
INSERT INTO log_name_links
SELECT r.*
  FROM link_demotion_backup b
 CROSS JOIN LATERAL jsonb_populate_record(NULL::log_name_links, b.before) r
 WHERE b.kind = 'link'
   AND b.after IS NULL
   AND NOT EXISTS (
       SELECT 1 FROM log_name_links l
        WHERE l.broadcast_artist_id = r.broadcast_artist_id AND l.artist_id = r.artist_id
   );

-- Statuses 0045 wrote, still as it wrote them: the name's status before.
UPDATE broadcast_artists ba
   SET match_status  = b.before ->> 'match_status',
       reason_code   = b.before ->> 'reason_code',
       reason_detail = b.before ->> 'reason_detail'
  FROM link_demotion_backup b
 WHERE b.kind = 'name'
   AND b.id = ba.id
   AND ba.match_status = b.after ->> 'match_status'
   AND ba.reason_code IS NOT DISTINCT FROM b.after ->> 'reason_code'
   AND ba.reason_detail IS NOT DISTINCT FROM b.after ->> 'reason_detail';

DROP TABLE IF EXISTS log_name_placeholders;
DROP TABLE IF EXISTS link_demotion_backup;
DELETE FROM schema_migrations WHERE version = '0045_link_demotion';
