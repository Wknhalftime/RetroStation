-- The work merge log, work merges phase 3b PR 3 (SPEC S3, S10, Q18; design C58; AUD-R079).
-- The runner wraps this file in one transaction.
-- Rollback: backend/db/rollback_0054_work_merge_log.sql; S10's restore of the logged merges is
-- backend/db/rollback_work_merges.sql.
SET LOCAL lock_timeout = '5s';

-- One row per guarded merge that went through, failed, or was rolled back. No foreign keys:
-- the works it names may be gone, and 0051's rollback drops performances.
CREATE TABLE work_merges (
    id         UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    seq        BIGINT      GENERATED ALWAYS AS IDENTITY,
    target_id  TEXT        NOT NULL,
    source_ids TEXT[]      NOT NULL,
    reason     TEXT        NOT NULL,
    approvals  JSONB       NOT NULL DEFAULT '[]',
    status     TEXT        NOT NULL,
    error      TEXT,
    logged_at  TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT work_merges_reason_known
        CHECK (reason IN ('library', 'merge list', 'musicbrainz work link')),
    CONSTRAINT work_merges_status_known CHECK (status IN ('merged', 'failed', 'rolled_back')),
    CONSTRAINT work_merges_error_when_failed CHECK ((status = 'failed') = (error IS NOT NULL))
);
CREATE INDEX idx_work_merges_target ON work_merges (target_id);

-- S10: the rollback map. One entry per row a merge changed, keyed by its table and key, with
-- the row as it was (NULL for a row the merge inserted). Filled by the capture trigger below
-- while a merge runs.
CREATE TABLE work_merge_rows (
    merge_id   UUID  NOT NULL REFERENCES work_merges (id) ON DELETE CASCADE,
    ord        BIGINT GENERATED ALWAYS AS IDENTITY,
    table_name TEXT  NOT NULL,
    row_key    JSONB NOT NULL,
    before     JSONB,
    PRIMARY KEY (merge_id, table_name, row_key)
);

-- S3: each merged-away work id, and the live work it redirects to.
CREATE TABLE work_redirects (
    old_id   TEXT PRIMARY KEY,
    new_id   TEXT NOT NULL,
    merge_id UUID NOT NULL REFERENCES work_merges (id) ON DELETE CASCADE
);
CREATE INDEX idx_work_redirects_new ON work_redirects (new_id);

-- The capture: while a merge runs, the guard sets retrostation.merge_id (set_config, local to
-- the transaction); each row a merge writes is recorded once, as it was before the merge.
CREATE FUNCTION work_merge_capture() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    merging UUID := current_setting('retrostation.merge_id')::uuid;
    image JSONB;
    keyed JSONB;
BEGIN
    IF TG_OP = 'INSERT' THEN
        image := to_jsonb(NEW);
    ELSE
        image := to_jsonb(OLD);
        IF TG_OP = 'UPDATE' AND image = to_jsonb(NEW) THEN
            RETURN NULL;
        END IF;
    END IF;
    SELECT jsonb_agg(image -> k ORDER BY n) INTO keyed
    FROM unnest(TG_ARGV) WITH ORDINALITY AS u (k, n);
    INSERT INTO work_merge_rows AS w (merge_id, table_name, row_key, before)
    VALUES (merging, TG_TABLE_NAME, keyed, CASE WHEN TG_OP = 'INSERT' THEN NULL ELSE image END)
    ON CONFLICT ON CONSTRAINT work_merge_rows_pkey DO NOTHING;
    RETURN NULL;
END
$$;

CREATE TRIGGER work_merge_capture AFTER INSERT OR UPDATE OR DELETE ON artists FOR EACH ROW
    WHEN (current_setting('retrostation.merge_id', true) <> '')
    EXECUTE FUNCTION work_merge_capture('id');
CREATE TRIGGER work_merge_capture AFTER INSERT OR UPDATE OR DELETE ON artist_names FOR EACH ROW
    WHEN (current_setting('retrostation.merge_id', true) <> '')
    EXECUTE FUNCTION work_merge_capture('id');
CREATE TRIGGER work_merge_capture AFTER INSERT OR UPDATE OR DELETE ON works FOR EACH ROW
    WHEN (current_setting('retrostation.merge_id', true) <> '')
    EXECUTE FUNCTION work_merge_capture('id');
CREATE TRIGGER work_merge_capture AFTER INSERT OR UPDATE OR DELETE ON recordings FOR EACH ROW
    WHEN (current_setting('retrostation.merge_id', true) <> '')
    EXECUTE FUNCTION work_merge_capture('id');
CREATE TRIGGER work_merge_capture AFTER INSERT OR UPDATE OR DELETE ON library_files FOR EACH ROW
    WHEN (current_setting('retrostation.merge_id', true) <> '')
    EXECUTE FUNCTION work_merge_capture('id');
CREATE TRIGGER work_merge_capture AFTER INSERT OR UPDATE OR DELETE ON performances FOR EACH ROW
    WHEN (current_setting('retrostation.merge_id', true) <> '')
    EXECUTE FUNCTION work_merge_capture('id');
CREATE TRIGGER work_merge_capture AFTER INSERT OR UPDATE OR DELETE ON performance_credits
    FOR EACH ROW WHEN (current_setting('retrostation.merge_id', true) <> '')
    EXECUTE FUNCTION work_merge_capture('performance_id', 'position');
CREATE TRIGGER work_merge_capture AFTER INSERT OR UPDATE OR DELETE ON song_masters FOR EACH ROW
    WHEN (current_setting('retrostation.merge_id', true) <> '')
    EXECUTE FUNCTION work_merge_capture('id');
CREATE TRIGGER work_merge_capture AFTER INSERT OR UPDATE OR DELETE ON format_overrides
    FOR EACH ROW WHEN (current_setting('retrostation.merge_id', true) <> '')
    EXECUTE FUNCTION work_merge_capture('id');
CREATE TRIGGER work_merge_capture AFTER INSERT OR UPDATE OR DELETE ON version_entry_states
    FOR EACH ROW WHEN (current_setting('retrostation.merge_id', true) <> '')
    EXECUTE FUNCTION work_merge_capture('work_id', 'version_text');
CREATE TRIGGER work_merge_capture AFTER INSERT OR UPDATE OR DELETE ON matches FOR EACH ROW
    WHEN (current_setting('retrostation.merge_id', true) <> '')
    EXECUTE FUNCTION work_merge_capture('id');
CREATE TRIGGER work_merge_capture AFTER INSERT OR UPDATE OR DELETE ON credit_pick_proposals
    FOR EACH ROW WHEN (current_setting('retrostation.merge_id', true) <> '')
    EXECUTE FUNCTION work_merge_capture('id');
