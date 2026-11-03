-- Artist model phase 1c (SPEC S1, S5, S6; M22, C7, C35, C36; AUD-R038, AUD-R039): the fuzzy
-- links are demoted to suggestions, and links to placeholders go. Lines and song matches are not
-- touched, so the demoted names' songs keep playing (AUD-R022 D2). Rollback:
-- backend/db/rollback_0045_link_demotion.sql, which reads link_demotion_backup.
SET LOCAL lock_timeout = '5s';

-- E5: the tables written here, locked first: ingest's one (broadcast_artists) in ingest's
-- order, and the names before their links, as the matcher and Resolve write them. A timeout
-- fails the deploy loudly.
LOCK TABLE broadcast_artists, log_name_links IN SHARE ROW EXCLUSIVE MODE;

-- S1: every link and name changed or deleted here, before and after (after NULL: deleted),
-- with why. It stays until Lance accepts 1c.
CREATE TABLE link_demotion_backup (
    kind   TEXT  NOT NULL,
    id     UUID  NOT NULL,
    why    TEXT  NOT NULL,
    before JSONB NOT NULL,
    after  JSONB,
    PRIMARY KEY (kind, id),
    CONSTRAINT link_demotion_backup_kind_known CHECK (kind IN ('link', 'name')),
    CONSTRAINT link_demotion_backup_why_known
        CHECK (why IN ('placeholder', 'listed', 'rejected_name', 'demoted', 'removed', 'status'))
);

-- S6, C7: station-log names that are programming, not performers. Exact cleaned names only:
-- "38 Special" and "Traffic" are real acts. A listed name is never linked or matched.
CREATE TABLE log_name_placeholders (
    normalized_name TEXT        PRIMARY KEY,
    added_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    added_by        TEXT        NOT NULL,
    CONSTRAINT log_name_placeholders_added_by_known CHECK (added_by IN ('migration', 'curator'))
);

INSERT INTO log_name_placeholders (normalized_name, added_by) VALUES
    ('spot break', 'migration'),
    ('special programming', 'migration'),
    ('syndicated program', 'migration'),
    ('various', 'migration'),
    ('various artists', 'migration');

-- The links to change, in this order of precedence. C7: any link to a MusicBrainz placeholder
-- (SPECIAL_PURPOSE_MBIDS in services/artist_linking_service.py), and any link of a listed name,
-- is deleted, whatever its state. The safety net: an active link of a rejected name becomes
-- rejected (0044 already copied them so). M22: an active legacy link of v0 tier normalization
-- under strength 95 becomes a suggestion if its old score is 56 or more, and goes otherwise.
INSERT INTO link_demotion_backup (kind, id, why, before, after)
SELECT 'link',
       p.id,
       p.why,
       p.before,
       CASE p.why WHEN 'rejected_name' THEN jsonb_set(p.before, '{state}', '"rejected"'::jsonb)
                  WHEN 'demoted' THEN jsonb_set(p.before, '{state}', '"suggested"'::jsonb) END
  FROM (
      SELECT l.id,
             to_jsonb(l) AS before,
             CASE
                 WHEN a.mbid = ANY (ARRAY[
                     '89ad4ac3-39f7-470e-963a-56509c546377',
                     '125ec42a-7229-4250-afc5-e057484327fe',
                     'eec63d3c-3b81-4ad4-b1e4-7c147d4d2b61',
                     '314e1c25-dde7-4e4d-b2f4-0a7b9f7c56dc',
                     'f731ccc4-e22a-43af-a747-64213329e088',
                     '9be7f096-97ec-4615-8957-8d40b5dcbc41',
                     '33cf029c-63b0-41a0-9855-be2a3665fb3b',
                     'a0ef7e1d-44ff-4039-9435-7d5fefdeecc9',
                     '80a8851f-444c-4539-892b-ad2a49292aa9',
                     '66ea0139-149f-4a0c-8fbf-5ea9ec4a6e49',
                     '90068d37-bae7-4292-be4a-704c145bd616',
                     '7e84f845-ac16-41fe-9ff8-df12eb32af55'
                 ]) THEN 'placeholder'
                 WHEN ph.normalized_name IS NOT NULL THEN 'listed'
                 WHEN l.state = 'active'
                      AND ba.match_status IN ('manual_rejected', 'auto_rejected')
                     THEN 'rejected_name'
                 WHEN l.state = 'active'
                      AND l.origin = 'legacy'
                      AND l.evidence -> 'legacy' ->> 'tier' = 'normalization'
                      AND l.strength < 95
                     THEN CASE WHEN (l.evidence -> 'legacy' ->> 'score')::float8 >= 56
                               THEN 'demoted' ELSE 'removed' END
             END AS why
        FROM log_name_links l
        JOIN broadcast_artists ba ON ba.id = l.broadcast_artist_id
        JOIN artists a ON a.id = l.artist_id
        LEFT JOIN log_name_placeholders ph ON ph.normalized_name = ba.normalized_name
  ) p
 WHERE p.why IS NOT NULL;

DELETE FROM log_name_links l
 USING link_demotion_backup b
 WHERE b.kind = 'link' AND b.id = l.id AND b.after IS NULL;

UPDATE log_name_links l
   SET state = b.after ->> 'state', updated_at = now()
  FROM link_demotion_backup b
 WHERE b.kind = 'link' AND b.id = l.id AND b.after IS NOT NULL;

-- S1, S3's rule: each name whose links changed takes its status from the links it holds now.
-- A rejected name, and a listed one (the list hides it), keep theirs. The detail says why.
INSERT INTO link_demotion_backup (kind, id, why, before, after)
SELECT 'name',
       s.id,
       'status',
       jsonb_build_object(
           'match_status', s.match_status,
           'reason_code', s.reason_code,
           'reason_detail', s.reason_detail
       ),
       jsonb_build_object(
           'match_status', s.new_status,
           'reason_code', s.new_reason,
           'reason_detail', s.new_detail
       )
  FROM (
      SELECT ba.id,
             ba.match_status,
             ba.reason_code,
             ba.reason_detail,
             CASE WHEN x.manual THEN 'manual_matched'
                  WHEN x.active THEN 'auto_matched'
                  WHEN x.suggested > 0 THEN 'needs_review'
                  WHEN ba.match_status IN ('auto_matched', 'manual_matched') THEN 'needs_review'
                  ELSE ba.match_status END AS new_status,
             CASE WHEN x.manual OR x.active THEN NULL
                  WHEN x.suggested > 0 THEN 'LINK_SUGGESTED'
                  WHEN ba.match_status IN ('auto_matched', 'manual_matched') THEN 'LINK_REMOVED'
                  ELSE ba.reason_code END AS new_reason,
             CASE WHEN x.manual OR x.active THEN NULL
                  WHEN x.suggested > 0
                      THEN 'Its automatic link by a close name is now a suggestion (M22): '
                           || x.suggested || ' suggested artist'
                           || CASE WHEN x.suggested = 1 THEN '' ELSE 's' END
                           || ', best ' || x.best || '%'
                  WHEN ba.match_status IN ('auto_matched', 'manual_matched') AND x.placeholder
                      THEN 'Its link to a placeholder artist was removed (C7): pick an artist'
                  WHEN ba.match_status IN ('auto_matched', 'manual_matched')
                      THEN 'Its automatic link by a close name scored under 56% and was removed'
                           || ' (M22): pick an artist'
                  ELSE ba.reason_detail END AS new_detail
        FROM broadcast_artists ba
        CROSS JOIN LATERAL (
            SELECT bool_or(l.state = 'active' AND l.origin = 'manual') IS TRUE AS manual,
                   bool_or(l.state = 'active') IS TRUE AS active,
                   count(*) FILTER (WHERE l.state = 'suggested') AS suggested,
                   max(l.strength) FILTER (WHERE l.state = 'suggested') AS best,
                   EXISTS (
                       SELECT 1 FROM link_demotion_backup b
                        WHERE b.kind = 'link' AND b.why = 'placeholder'
                          AND (b.before ->> 'broadcast_artist_id')::uuid = ba.id
                   ) AS placeholder
              FROM log_name_links l
             WHERE l.broadcast_artist_id = ba.id
        ) x
       WHERE ba.match_status NOT IN ('manual_rejected', 'auto_rejected')
         AND NOT EXISTS (
             SELECT 1 FROM log_name_placeholders ph WHERE ph.normalized_name = ba.normalized_name
         )
         AND EXISTS (
             SELECT 1 FROM link_demotion_backup b
              WHERE b.kind = 'link' AND (b.before ->> 'broadcast_artist_id')::uuid = ba.id
         )
  ) s
 WHERE (s.new_status, s.new_reason, s.new_detail)
       IS DISTINCT FROM (s.match_status, s.reason_code, s.reason_detail);

UPDATE broadcast_artists ba
   SET match_status  = b.after ->> 'match_status',
       reason_code   = b.after ->> 'reason_code',
       reason_detail = b.after ->> 'reason_detail'
  FROM link_demotion_backup b
 WHERE b.kind = 'name' AND b.id = ba.id;

ANALYZE link_demotion_backup;
ANALYZE log_name_links;
