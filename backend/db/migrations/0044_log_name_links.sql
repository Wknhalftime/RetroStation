-- Artist model phase 1a (C30, C45, C50; AUD-R035): log-name links get their own table, and
-- every artist row of matches is copied into it. The old rows stay in matches, unread and
-- unwritten, until phase 1c drops them (M35). Rollback: backend/db/rollback_0044_log_name_links.sql.
SET LOCAL lock_timeout = '5s';

-- One row per (log name, artist) in every state, so a rejected pair stays remembered (C19).
-- No cascade on either key: a log name or an artist with links is not deleted from under them.
CREATE TABLE log_name_links (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    broadcast_artist_id UUID        NOT NULL REFERENCES broadcast_artists (id),
    artist_id           TEXT        NOT NULL REFERENCES artists (id),
    mbid                TEXT,
    state               TEXT        NOT NULL,
    origin              TEXT        NOT NULL,
    strength            SMALLINT    NOT NULL,
    evidence            JSONB       NOT NULL,
    credit_part         SMALLINT    NOT NULL DEFAULT 0,
    role                TEXT        NOT NULL DEFAULT 'main',
    join_after          TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT log_name_links_name_artist_key UNIQUE (broadcast_artist_id, artist_id),
    CONSTRAINT log_name_links_state_known CHECK (state IN ('active', 'suggested', 'rejected')),
    CONSTRAINT log_name_links_origin_known
        CHECK (origin IN ('automatic', 'manual', 'one_time_pass', 'legacy')),
    CONSTRAINT log_name_links_strength_range CHECK (strength BETWEEN 0 AND 100),
    CONSTRAINT log_name_links_evidence_versioned
        CHECK (jsonb_typeof(evidence) = 'object' AND evidence ? 'version'),
    CONSTRAINT log_name_links_credit_part_known CHECK (credit_part >= 0),
    CONSTRAINT log_name_links_role_known CHECK (role IN ('main', 'featured'))
);

CREATE INDEX idx_log_name_links_artist_id ON log_name_links (artist_id);

-- The stubs this copy makes, for the rollback; dropped in phase 1c.
CREATE TABLE link_copy_stubs (
    artist_id TEXT PRIMARY KEY REFERENCES artists (id) ON DELETE CASCADE
);

-- S3: one stub artist for each target MBID no catalog artist holds, by id or by MBID.
-- Enrichment names it later (phase 1a PR 2); until then its name is the MBID.
WITH unknown AS (
    SELECT DISTINCT m.target_id AS mbid
      FROM matches m
     WHERE m.artist_id IS NOT NULL
       AND COALESCE(m.target_id, '') <> ''
       AND NOT EXISTS (
           SELECT 1 FROM artists a WHERE a.id = m.target_id OR a.mbid = m.target_id
       )
), stubs AS (
    INSERT INTO artists (id, name, sort_name, normalized_name, mbid, origin, needs_enhancement)
    SELECT gen_random_uuid()::text, mbid, mbid, NULL, mbid, 'musicbrainz', TRUE
      FROM unknown
    RETURNING id
)
INSERT INTO link_copy_stubs (artist_id)
SELECT id FROM stubs;

-- S2: one link per (log name, artist). The target resolves by catalog id, else by MBID.
-- Origin manual for a manual-tier row on a manual_matched name, else legacy; a rejected name's
-- links are rejected. A manual-tier row's score (1.0) is 100. Duplicate pairs collapse to one:
-- the manual one first, then the earliest. Evidence v0 keeps the old row (C50).
INSERT INTO log_name_links
    (broadcast_artist_id, artist_id, mbid, state, origin, strength, evidence, created_at)
SELECT DISTINCT ON (r.broadcast_artist_id, r.artist_id)
       r.broadcast_artist_id,
       r.artist_id,
       r.mbid,
       CASE WHEN r.name_status IN ('manual_rejected', 'auto_rejected')
            THEN 'rejected' ELSE 'active' END,
       CASE WHEN r.manual THEN 'manual' ELSE 'legacy' END,
       CASE WHEN r.match_tier = 'manual'
            THEN 100 ELSE round(r.confidence_score::numeric) END,
       jsonb_build_object(
           'version', 0,
           'legacy', jsonb_build_object(
               'match_id', r.match_id,
               'target_id', r.target_id,
               'tier', r.match_tier,
               'score', r.confidence_score,
               'created_at', r.created_at
           )
       ),
       r.created_at
  FROM (
      SELECT m.artist_id AS broadcast_artist_id,
             a.id AS artist_id,
             a.mbid,
             ba.match_status AS name_status,
             m.match_tier = 'manual' AND ba.match_status = 'manual_matched' AS manual,
             m.match_tier,
             m.confidence_score,
             m.id AS match_id,
             m.target_id,
             m.created_at
        FROM matches m
        JOIN broadcast_artists ba ON ba.id = m.artist_id
       CROSS JOIN LATERAL (
           SELECT c.id, c.mbid
             FROM artists c
            WHERE c.id = m.target_id OR c.mbid = m.target_id
            ORDER BY (c.id = m.target_id) DESC
            LIMIT 1
       ) a
       WHERE m.artist_id IS NOT NULL
         AND COALESCE(m.target_id, '') <> ''
  ) r
 ORDER BY r.broadcast_artist_id, r.artist_id, r.manual DESC, r.created_at, r.match_id;

ANALYZE artists;
ANALYZE link_copy_stubs;
ANALYZE log_name_links;
